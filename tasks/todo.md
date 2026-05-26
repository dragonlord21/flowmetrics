# Todo

Live checklist for the two plans in [plan.md](plan.md). Tick boxes as
each PR lands. Both plans now shipped end-to-end.

## Plan A — Portability ✅

### Slice A1: Cross-platform compatibility fixes ✅ (commit 6000b9e)

- [x] Branch `os.name` in `app.py` browser-backfill subprocess.
- [x] Add `tests/test_cross_platform_subprocess.py`.
- [x] OS-branch the `lsof`/`kill` hints in serve port-busy and
      samples-serve.
- [x] Add multi-OS CI matrix in `.github/workflows/test.yml`
      (ubuntu / macos / windows).
- [x] Cleared the 33 pre-existing ruff lints so CI starts green.

### Slice A2: Native scheduler recipes ✅ (commit bf6c858)

- [x] `flow materialise-all` wrapper command + 6 unit tests.
- [x] linux-systemd `.service` + `.timer` + README.
- [x] linux-cron `crontab.sample` + README.
- [x] macos-launchd `.plist` + README.
- [x] windows-task-scheduler `.xml` + README.
- [x] Top-level `scripts/scheduling/README.md` indexes them.

### Slice A3: `flow backup` / `flow restore` ✅ (commit 6f0db7e)

- [x] `src/flowmetrics/backup.py` — `.tar.gz` + header + SHA-256.
- [x] `flow backup` CLI command.
- [x] `flow restore` CLI command (verifies checksums before
      writing).
- [x] +10 tests: round-trip, refuse-without-force, corrupted
      tarball, tampered payload, cache exclude/include, default
      dated output.
- [x] `scripts/scheduling/backup/` wrappers (POSIX + PowerShell).

### Slice A4: Container path ✅ (commit 68e4726)

- [x] `Dockerfile` (multi-stage, non-root, slim).
- [x] `compose.yml` (serve + materialise services).
- [x] `.github/workflows/materialise.yml` (scheduled GH Actions
      ingest with artifact upload).
- [x] CI `docker` job builds + smokes `flow --help` inside the
      container.

### Slice A5: Ops guide ✅ (commit afc1143)

- [x] `docs/OPERATIONS.md` consolidating A1–A4.
- [x] README "Documentation" lists the new page.

## Plan B — Web-UI contract builder ✅

### Slice B1: Contract read API ✅ (commit 1322780)

- [x] `GET /api/internal/contracts` — list.
- [x] `GET /api/internal/contracts/{id}` — full payload (parsed +
      raw YAML + materialise status block).
- [x] Auth respects localhost-vs-network posture.
- [x] +9 unit tests; `_available_contracts` now picks up `.yml`
      alongside `.yaml`.

### Slice B2: Contract write API + validation ✅ (commit 6d38b63)

- [x] `POST /api/internal/contracts/_validate` — structured
      `{valid, errors[{message, line?, column?}]}`.
- [x] `PUT /api/internal/contracts/{id}` — atomic write.
- [x] `DELETE /api/internal/contracts/{id}` — refuses when
      Parquet exists; `?purge_data=true` wipes alongside.
- [x] CSRF guard (`X-Requested-With: fetch`) on every write.
- [x] +13 unit tests.

### Slice B3: New-contract wizard ✅ (commit eeed5ef)

- [x] Route + template `/admin/contracts/new`.
- [x] Source picker with conditional fields.
- [x] `POST /api/internal/contracts/_probe-source` (injectable
      callable; production uses httpx to HEAD the GitHub repo or
      GET the Jira project).
- [x] Save flow: validate → PUT → redirect.
- [x] "+ New workflow" CTA on `/`.
- [x] +6 unit tests; visually verified in the browser.

### Slice B4: Stage builder via probe materialise ✅ (commit 254fe39)

- [x] `POST /api/internal/contracts/_probe-stages` with 15-min
      per-target cache + `?force=true` bust.
- [x] Stages fieldset with three click-to-move buckets +
      "+ Add custom stage" free-form input.
- [x] Save persists the full `states:` block via the existing
      PUT.
- [x] +7 unit tests.

### Slice B5: Edit existing contract ✅ (commit 2b7397d)

- [x] Route + template `/admin/contracts/{id}/edit` (reuses
      wizard with `mode=edit`).
- [x] Hydrates every field from GET on load; locks the id.
- [x] Delete affordance with name-typing prompt + warehouse
      purge confirmation.
- [x] "edit" link in the data-source strip on every dashboard.
- [x] +5 unit tests.
