"""igctl entry point: global options and sub-command registration."""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from igctl import __version__
from igctl.backends.base import IgctlError
from igctl.cli import analyze, auth, automate, dm, download, engage, post, schedule
from igctl.core.context import Context

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="igctl",
    help="Publish, analyze and automate Instagram from the terminal.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

app.add_typer(auth.app, name="auth", help="Log in, list and switch accounts.")
app.add_typer(post.app, name="post", help="Publish photos, videos, reels, carousels, stories.")
app.add_typer(schedule.app, name="schedule", help="Queue posts and run the scheduler.")
app.add_typer(analyze.app, name="analyze", help="Profile, post, hashtag and follower analytics.")
app.add_typer(engage.app, name="engage", help="Like, comment, follow and moderate.")
app.add_typer(dm.app, name="dm", help="Read and send direct messages.")
app.add_typer(download.app, name="download", help="Download posts, profiles and stories.")
app.add_typer(automate.app, name="automate", help="Run YAML-defined automation rules.")


def _version(value: bool) -> None:
    if value:
        console.print(f"igctl {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    account: str | None = typer.Option(None, "--account", "-a", help="Account alias to use."),
    backend: str | None = typer.Option(
        None, "--backend", "-b", help="Force a backend: graph | private."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen; change nothing."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    if backend not in (None, "graph", "private"):
        raise typer.BadParameter("backend must be 'graph' or 'private'")
    ctx.obj = Context(
        account_name=account, backend_override=backend, dry_run=dry_run, json_output=json_output
    )


def run() -> None:
    try:
        app()
    except IgctlError as e:
        err_console.print(f"[red]error:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()
