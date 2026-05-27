# Plans: Portability & Web Contract Builder

Two independent plans, each broken into vertical slices that ship value
on their own. Save artifacts as PRs that can be reviewed and merged in
slice order; nothing in one plan blocks the other.

Working assumptions (call out anything that's wrong):

- **Cross-platform scope** = macOS, Linux, Windows.
- **Containers** are included as an alternate path (one Dockerfile + one
  compose recipe + one GitHub Actions example), not the primary path.
- **Backup target** defaults to local-disk `rsync`/`restic` with one
  cloud (S3-compatible) example; not S3-only.
- **Auth for the web contract builder** rides on the existing
  `--password` (HTTP Basic via `--host 0.0.0.0`); localhost-bound use
  stays unauthenticated.
- **Stage discovery** in the builder uses a *probe materialise* — small
  bounded fetch that returns known stages from the source — with
  manual override always available.

---

## Plan A: Portability — automate, back up, restore on any OS

### Goal

A non-developer on macOS, Linux, or Windows can:

1. Install flowmetrics.
2. Drop a workflow YAML into `contracts/`.
3. Schedule a daily materialise via their OS's native scheduler **or** a
   container.
4. Back up the warehouse to disk or object storage.
5. Restore from that backup onto a fresh machine and serve unchanged.

### Why now

The materialise CLI is already cron-friendly (exit codes, atomic
writes, manifests). The gaps are surface-level: one Windows subprocess
bug, missing schedule templates for each OS, no backup story, and ops
docs scattered across HOWTO + SPEC. Closing them is mostly docs +
scripts + one platform-fix; ~1 week of focused work.

### Dependency graph

```
Slice A1 ──┐
           ▼
Slice A2 ──→ Slice A3 ──→ Slice A5
           ▼               (docs)
Slice A4 ──┘
```

`A1` (Windows + cross-platform fixes) unblocks anything that runs on
Windows. `A2` (scheduler templates) and `A4` (container) each ship a
working automation path on their own. `A3` (backup/restore) only needs
A1. `A5` consolidates docs once the recipes exist.

### Slices

#### A1 — Cross-platform compatibility fixes

**One PR. Unblocks Windows users.**

- Patch `app.py` browser-triggered backfill: use
  `start_new_session=True` on POSIX and
  `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows. Single
  branch on `os.name`. Add a unit test that exercises both branches via
  monkey-patched `os.name` (mock the Popen call; assert kwargs).
- Replace POSIX-only debugging hints in CLI error messages
  (`lsof -ti:PORT`, `kill …`) with a cross-platform block: POSIX hint
  on POSIX, Windows hint (`netstat -ano | findstr :PORT`,
  `taskkill /F /PID PID`) on Windows. Same `os.name` check.
- Document that `flow serve` and `flow materialise` both work on
  Windows by exercising the existing test suite under `windows-latest`
  in CI (add a Windows job to `.github/workflows/test.yml`; mark any
  CI-only-broken tests `@pytest.mark.skipif(os.name == 'nt')` with a
  TODO link to a follow-up issue).

**Files touched**: `src/flowmetrics/app.py`, `src/flowmetrics/cli.py`,
`src/flowmetrics/serve.py`, new `tests/test_cross_platform_subprocess.py`,
`.github/workflows/test.yml`.

**Acceptance**

- `flow serve` + `flow materialise` complete without error on
  `windows-latest` in CI.
- Browser-triggered backfill returns the expected JSON status
  transitions on Windows (mocked subprocess test).
- Port-busy error message branches by OS; both branches asserted by
  unit test.

**Verification**

- New unit tests pass locally (`uv run pytest tests/test_cross_platform_subprocess.py`).
- CI green on the new windows-latest matrix entry.
- Manual: run `flow serve` on a Windows VM, hit `/data-source`,
  trigger a backfill — status file transitions running → done.

#### A2 — Native scheduler recipes (cron / launchd / Task Scheduler)

**One PR. Ships a working scheduled-ingest on every supported OS.**

- Add `scripts/scheduling/` with three minimal, parameterised templates:
  - `linux-systemd/flowmetrics-materialise.service` +
    `flowmetrics-materialise.timer` (daily, 02:30 local).
  - `linux-cron/crontab.sample` (daily, same time; safe `PATH` + `cd`
    preamble).
  - `macos-launchd/com.flowmetrics.materialise.plist`
    (`StartCalendarInterval` daily 02:30; `StandardErrorPath`
    points at a log file).
  - `windows-task-scheduler/flowmetrics-materialise.xml` (importable
    via `schtasks /Create /XML`) + a one-page README on what to edit.
- Each template uses environment-variable substitution
  (`FLOWMETRICS_HOME`, `FLOWMETRICS_VENV`, `FLOWMETRICS_WORKFLOW`) so
  copying + setting two vars is the whole install.
- Add a "one liner" wrapper script per OS that materialises every YAML
  in `contracts/` so the scheduler only fires one command
  (`scripts/scheduling/materialise-all.sh` + `.ps1`). Wrapper iterates,
  logs successes/failures to a single JSON manifest per day, exits
  non-zero only if every contract failed (so monitoring alerts
  meaningfully).
- README per directory explaining install steps, log inspection, and
  how to dry-run.

**Files touched**: new `scripts/scheduling/**`, no source code.

**Acceptance**

- Each template runs successfully on its native OS using the demo
  `astral-uv-week` contract (manual verification documented in the
  PR description).
- Wrapper script writes a `_status/daily-{date}.json` with per-contract
  outcomes and exits 0 when at least one contract succeeded.
- Templates parameterised with the same three env vars; no
  hard-coded paths.

**Verification**

- Linux: install timer in a Docker container running systemd, advance
  the clock, confirm the run fired and produced fresh Parquet.
- macOS: load plist via `launchctl bootstrap gui/$UID`, trigger via
  `launchctl kickstart`, confirm log file populated.
- Windows: import XML via `schtasks /Create /XML`, trigger via
  `schtasks /Run`, confirm warehouse updated.

#### A3 — Backup & restore (`flow backup` / `flow restore`)

**One PR. Single command for warehouse portability.**

- Add `flow backup --data-dir DATA --output PATH [--include-cache]`.
  Output is a single timestamped `.tar.zst` (zstd: fast, well-supported).
  Contents: the entire warehouse (`work_items/`, `transitions/`,
  `runs/`), plus a `flowmetrics-backup.json` header with:
  - schema version (`flowmetrics.backup.v1`),
  - flowmetrics version,
  - DuckDB version (Parquet writer),
  - source contract names + sizes,
  - SHA-256 of every file for integrity.
  Cache is excluded by default (re-fetchable); `--include-cache` flips it.
- Add `flow restore --input PATH --data-dir DATA [--force]`. Verifies
  the header schema + checksums before extracting; refuses to overwrite
  a non-empty `--data-dir` unless `--force`.
- `flow backup --target s3://bucket/prefix` writes the same tarball
  via `boto3` when present, with `--profile NAME` honoring the AWS
  shared-credentials file. Use `botocore`'s standard env vars; never
  bake creds into config.
- Add scheduler templates in `scripts/scheduling/backup/` for each OS,
  running `flow backup` after the daily materialise.
- Unit tests: round-trip a tiny synthetic warehouse — backup → restore
  → DuckDB read returns identical rows. Test that restore refuses to
  overwrite non-empty dir without `--force`. Test that a corrupted
  tarball (mutated bytes) fails fast with a clear error.

**Files touched**: new `src/flowmetrics/backup.py`, `src/flowmetrics/cli.py`
(2 new commands), `tests/test_backup_restore.py`, scheduler templates.

**Acceptance**

- A backup of a 100MB warehouse + restore on a fresh `--data-dir`
  produces a byte-identical (per checksum) DuckDB read.
- S3 mode actually uploads when `boto3` is installed and credentials
  are available; fails with a clear "boto3 not installed" message
  otherwise.
- Corrupted tarball restore fails before extraction with an
  actionable error.

**Verification**

- `uv run pytest tests/test_backup_restore.py` — round-trip tests pass.
- Manual S3 round-trip against MinIO (documented in PR description).
- Manual full-disk recovery scenario: corrupt `data/`, restore from
  yesterday's tarball, serve, charts render unchanged.

#### A4 — Container path (Dockerfile + compose + GH Actions CronJob)

**One PR. Alternate automation path; ops-team-friendly.**

- `Dockerfile` (multi-stage: `uv sync --frozen` in builder, slim runtime
  with only the venv + the package). Single image runs either
  `flow materialise` or `flow serve` based on `CMD`. Image labels:
  `org.opencontainers.image.source`, `…version`.
- `compose.yml`: two services — `materialise` (one-shot, run via
  `docker compose run`) and `serve` (long-running, port 8000, bind-mounts
  `./contracts` and `./data`). Documented as the "I just want it
  running" path.
- `.github/workflows/materialise.yml`: scheduled GH Actions workflow
  that runs `flow materialise` against contracts committed in the repo
  (uses repo as the source of truth for YAML; warehouse persists to
  a GH cache / artifact). Demonstrates the cloud-native pattern.
- Test job in CI that builds the image and runs
  `docker run --rm flowmetrics flow --help` — catches missing dependencies
  in the slim base.

**Files touched**: new `Dockerfile`, new `compose.yml`,
new `.github/workflows/materialise.yml`, CI updates.

**Acceptance**

- Image is < 250 MB.
- `docker compose up serve` brings up the dashboard against a
  bind-mounted `data/` directory.
- The GH Actions workflow runs on a manual trigger end-to-end against a
  sample contract.

**Verification**

- `docker build .` + `docker run` health-checks in CI.
- Manual: `docker compose up serve`, open `http://localhost:8000`,
  verify a tile renders.
- Manual: `gh workflow run materialise.yml`, inspect logs + the
  saved artifact.

#### A5 — Ops guide on GitHub Pages

**One PR. Consolidates Slices A1–A4 into a coherent doc.**

- New `docs/OPERATIONS.md`, linked from the README's "Documentation"
  list. Sections:
  1. *Two automation paths* — native scheduler vs. container; picker
     based on the reader's situation (one server vs. fleet, prefer
     systemd vs. K8s).
  2. *Cross-platform install* — three short paths (macOS/Linux/Windows),
     each ending with "you should now be able to run `flow --help`".
  3. *Daily ingest* — one subsection per OS, each linking to the
     templates in `scripts/scheduling/`.
  4. *Backup & restore* — `flow backup` + `flow restore`, the on-disk
     layout, recovery scenarios (lost warehouse, lost machine,
     migrating between machines).
  5. *Containers* — Docker + compose + GH Actions, copy-pasteable.
  6. *Troubleshooting* — "warehouse won't read after upgrade",
     "stale lock file", "subprocess hangs", with the fix command.
- Update `docs/HOWTO.md`: prune cron / backup content out (it's now in
  OPERATIONS); leave HOWTO as the *getting-started* doc.
- Update README's "Documentation" section to point at the new page.
- Verify Jekyll renders the new page and its internal links resolve.

**Files touched**: `docs/OPERATIONS.md`, `docs/HOWTO.md`, `README.md`.

**Acceptance**

- A new reader following only `docs/OPERATIONS.md` can stand up a
  scheduled materialise on each OS.
- Internal markdown links resolve on github.com AND on the published
  Pages site (`jekyll-relative-links` handles the rewrite — verify
  visually after publish).
- README "Documentation" lists the new doc.

**Verification**

- `bundle exec jekyll serve` locally; click every link in OPERATIONS.
- Push to main, wait for Pages build, click every link on the live
  site.

### Phase checkpoints (Plan A)

- **After A1**: Windows is on the support matrix. Pause for review
  before scheduling-template work — ensures the platform-fix touched
  the right places.
- **After A2 + A3**: Daily ingest + backup story exist as installable
  artifacts. Pause for review before container work — confirms native
  path is the priority.
- **After A4 + A5**: Both automation paths and docs are live. Final
  review before announcing.

---

## Plan B: Web-UI contract builder

### Goal

A user with the dashboard open can:

1. Create a new contract end-to-end without touching the filesystem.
2. Pick a source (GitHub / Jira), have the source validate the
   repo/project for them.
3. Build the workflow's `wip` / `done` stages by probing the source for
   known states (with manual override) and drag-reordering.
4. Save the result; the YAML lands in `--workflows-dir` and the new
   contract shows up in the dashboard immediately.
5. Edit an existing contract through the same UI.

### Why now

The dashboard renders contracts but treats them as read-only file-
system fixtures. SPEC §15.6 already lays out a "contract switcher +
editor" as Slice 6; this plan operationalises it without the YAML
textarea (which is friction for non-developers).

### Dependency graph

```
B1 (read API) ──┐
                ├──→ B3 (new-contract wizard) ──┐
B2 (write API) ─┘                               ├──→ B5 (edit page)
                                                │
                B4 (stage probe) ───────────────┘
```

`B1` and `B2` are the API foundation. `B3` is the first user-facing
slice (new contract). `B4` is the helper that makes the stage step
worth a UI. `B5` rounds it out with edit.

### Slices

#### B1 — Contract read API

**One PR. Exposes today's YAML through an HTTP read endpoint.**

- New endpoint `GET /api/internal/contracts` → list of `{id, label,
  source}` for every YAML under `--workflows-dir`. JSON.
- New endpoint `GET /api/internal/contracts/{id}` → full contract
  payload: parsed dataclass fields + the raw YAML text + a
  `materialise_status` block ({last_run_at, status, items}).
- Both are unauthenticated when serving on `127.0.0.1`; gated by the
  existing HTTP Basic when serving off-localhost.
- Unit tests: list returns every YAML; detail returns parsed +
  raw + status; both return 404 for unknown IDs.

**Files touched**: `src/flowmetrics/app.py`, new
`src/flowmetrics/web/api/contracts.py` (route module), tests.

**Acceptance**

- `curl http://localhost:8000/api/internal/contracts` lists the demo
  contract.
- `curl …/api/internal/contracts/astral-uv-week` returns the parsed
  fields, raw YAML, and last-run summary.
- Off-localhost calls without basic auth return 401.

**Verification**

- New unit tests pass.
- Manual `curl` round-trip against the running dev server.

#### B2 — Contract write API + server-side validation

**One PR. Round-trip via API; CLI not required.**

- `PUT /api/internal/contracts/{id}` — body is `{yaml: STRING}`.
  Validates by routing through the existing `load_contract` parser
  (no duplication). On success: writes `{id}.yaml` to `--workflows-dir`
  atomically (`tmp` → `os.replace`) and returns the parsed payload.
- `DELETE /api/internal/contracts/{id}` — removes the YAML. Refuses
  if Parquet for that contract exists unless body has
  `{purge_data: true}`. Returns 409 on conflict with a `hint` pointing
  at the purge option.
- Validation surface: `POST /api/internal/contracts/_validate` —
  takes `{yaml: STRING}`, returns `{valid: bool, errors: [{line,
  column, message}]}` without touching disk. Used live by the editor.
- Auth: same posture as B1 + a CSRF check on write methods (use
  FastAPI's middleware pattern; tie the token to the session cookie).
- Tests cover: round-trip create → list → read; validation surface
  matches CLI errors; delete refused when warehouse non-empty;
  CSRF block.

**Files touched**: `src/flowmetrics/web/api/contracts.py` (extend),
`src/flowmetrics/contract.py` (factor out a `validate_yaml_text` that
returns structured errors with line numbers), tests.

**Acceptance**

- `curl -X PUT …/contracts/foo --data '{"yaml":"contract:\n  name:
  foo\n  source: github\n  repo: owner/repo"}'` creates the file and
  returns 200 with the parsed payload.
- Invalid YAML returns 422 with line numbers in the error array.
- Delete refuses when data exists; succeeds with `purge_data: true`.

**Verification**

- Unit tests for each path.
- Manual: drive the API end-to-end with `curl`; confirm `contracts/`
  reflects writes; confirm the dashboard's workflow picker shows the
  new contract on the next page load.

#### B3 — New-contract wizard (source picker + repo/project validator)

**First user-facing slice. Source-only; stages come in B4.**

- New page `/admin/contracts/new` (FastAPI route + Jinja template).
  Three steps in a single form, no navigation between them — all
  visible at once for fast scanning:
  1. Name (slug) + label (human display).
  2. Source picker (radio: GitHub | Jira) → conditional fields:
     GitHub `repo` (owner/name); Jira `jira_url` + `jira_project`.
  3. Date window (`start`, `stop`), optional.
- On blur of the repo/project field, fires `POST
  /api/internal/contracts/_probe-source` with `{source, repo} ` (or
  `{source, jira_url, jira_project}`). Returns
  `{ok: bool, label?: str, error?: str}`. Inline check/cross.
- "Save & open" button validates the form against
  `_validate`, then `PUT`s, then redirects to the new workflow's
  dashboard.
- Empty `states:` is allowed at save time — stages are added in B4 or
  inferred at chart-render time (existing behavior).
- Add a "+ New contract" button to the workflow switcher on `/`.

**Files touched**: new
`src/flowmetrics/web/templates/contracts_new.html.jinja`,
`src/flowmetrics/web/api/contracts.py` (add `_probe-source`),
`src/flowmetrics/sources/{github,jira}.py` (export a `validate_target`
helper), template updates on the home page, tests.

**Acceptance**

- Wizard at `/admin/contracts/new` renders without an existing
  contract.
- Source probe returns a green check for `astral-sh/uv` and a red X +
  message for `does-not/exist`.
- Save creates the YAML, redirects, and the new dashboard renders.

**Verification**

- Unit test for `_probe-source` (mock the source adapter).
- E2E (Playwright): fill in form for `astral-sh/uv`, save, assert
  redirect to `/workflows/{name}` and the page returns 200.

#### B4 — Stage builder via probe materialise

**One PR. Replaces "freeform stage names" with discovery + DnD.**

- After the source step passes in B3, the wizard reveals a "Stages"
  section.
- "Discover stages" button calls a new endpoint `POST
  /api/internal/contracts/_probe-stages`. Server runs a bounded
  materialise (`--since` = last 30 days, no `--status-file`) into a
  scratch dir, reads the resulting transitions to extract distinct
  stage names, deletes the scratch dir. Returns
  `{stages: [name], hint?: str}` (the hint surfaces things like
  "no PRs in the last 30 days; widen the window").
- The UI shows three buckets (Backlog / WIP / Done) with discovered
  stages as draggable chips. Empty buckets allowed. Order = render
  order. Free-form "+ Add stage" input for custom names.
- "Save" persists via the existing `PUT` (the wizard now writes the
  full `states:` block).
- Probe results cache for 15 minutes per source so the user can iterate
  without re-paying the API call.
- Tests: stage probe returns sensible results against a recorded
  cassette; cache hits don't re-fetch; UI saves what the user dragged.

**Files touched**:
`src/flowmetrics/web/api/contracts.py` (new endpoint),
`src/flowmetrics/web/templates/contracts_new.html.jinja` (stage UI),
small JS module for drag-and-drop (vanilla — no React), tests.

**Acceptance**

- Probe on `astral-sh/uv` returns the expected GitHub PR stages
  (`Draft`, `Awaiting Review`, `Changes Requested`, `Approved`,
  `Merged`).
- The wizard saves a contract whose `states:` block matches the
  user's drag order.
- Probe failure (e.g. invalid repo at probe time) surfaces inline,
  doesn't block save with manual entry.

**Verification**

- Unit tests for the probe endpoint with mocked source.
- Playwright E2E: discover stages, drag two between buckets, save,
  read the YAML, assert order.

#### B5 — Edit existing contract

**Final slice. Closes the loop.**

- New page `/admin/contracts/{id}/edit`. Reuses the wizard template
  with a different mode flag (`mode: edit`). Pre-fills every field
  from the `GET` payload.
- Re-running the stage probe on an existing contract gives the user a
  diff: "discovered stages match your current `states:` (no change
  needed)" or "stages now include `Awaiting Review` — add to WIP?".
- "Save" routes through the same `PUT` (idempotent overwrite).
  "Delete" routes through `DELETE` + the confirmation dialog
  (the destructive action prompts for the contract name).
- Add an "Edit" link to the data-source strip in `_base.html.jinja`
  next to the workflow name.

**Files touched**: `contracts_edit.html.jinja` (or reuse-with-mode-flag
on `contracts_new.html.jinja`), `app.py` (new route), template
updates, tests.

**Acceptance**

- Editing the demo contract's label persists and reflects in the
  breadcrumb on the next page load.
- Stage diff highlights additions vs removals.
- Delete with no warehouse → succeeds; with warehouse → blocked
  until confirmed.

**Verification**

- Unit + E2E tests as above.
- Manual: rename `astral-uv-week` to `astral-uv-30d`, confirm files
  and dashboard.

### Phase checkpoints (Plan B)

- **After B1 + B2**: API round-trip works without UI. Pause for review
  to confirm the validation surface and CSRF posture before any HTML.
- **After B3**: New contracts can be created end-to-end (without
  stages). Pause for UX review on the wizard flow.
- **After B4**: Stages discoverable; the "you don't have to know
  GitHub PR labels" pitch is real. Pause to validate against a non-
  GitHub source (Jira) before edit.
- **After B5**: Plan complete. Final review.

### Non-goals (explicit)

- Multi-tenant auth, RBAC, per-user contracts. The existing single
  `--password` is the auth boundary.
- A YAML textarea editor. The structured form covers every field
  in the schema today; a raw editor is friction we don't need.
- Contract templating / cloning. Add when there's a third use case.
- Webhook-triggered re-materialise. The cron path is enough until
  someone asks.

---

## How to use this plan

1. Pick one plan to start with (A is lower-risk, ships docs +
   automation that everything else benefits from).
2. Work one slice at a time. Each slice is a self-contained PR with
   acceptance + verification in its description.
3. At each phase checkpoint, pause for human review; do not advance.
4. If the plan goes stale (something underneath changes, scope
   shifts), update this file first, then keep going.

The accompanying [tasks/todo.md](todo.md) is the live checklist —
tick boxes there as each slice lands.
