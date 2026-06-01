"""`python -m flowmetrics` → the `flow` CLI.

Lets the web app spawn `flow materialize` as a subprocess using
the same interpreter (`sys.executable -m flowmetrics`), for
browser-triggered backfills on the Data Source page.
"""

from flowmetrics.cli import cli

if __name__ == "__main__":
    cli()
