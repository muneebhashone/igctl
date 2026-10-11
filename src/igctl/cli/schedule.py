"""`igctl schedule`: queue posts for later and run the scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from igctl.backends.base import Backend, IgctlError
from igctl.cli._common import emit, emit_ok, err_console, get_ctx, ok, warn
from igctl.core.context import Context
from igctl.core.files import read_text_file
from igctl.core.media_host import check_graph_hostable
from igctl.core.models import MediaType, PublishRequest
from igctl.scheduler import queue
from igctl.scheduler.runner import (
    STALE_AFTER,
    AccountContext,
    RunOutcome,
    StopFlag,
    run_due,
    run_loop,
)
from igctl.scheduler.timeparse import format_local, parse_when

app = typer.Typer(no_args_is_help=True)

WHEN_HELP = (
    "When to publish: now, +30m, +2h, +1d, 'tomorrow 18:00', 'fri 9am', or ISO 2026-10-12T18:00."
)
TZ_HELP = "Time zone for naive times (IANA name, UTC or +05:00). Default: system local."

RUN_EPILOG = """\
[bold]Running unattended[/bold]

Either keep a daemon running ([cyan]igctl schedule run --loop[/cyan]) or have the OS call
[cyan]igctl schedule run --once[/cyan] every minute or so:

[bold]Windows Task Scheduler[/bold] (PowerShell):
[dim]schtasks /Create /TN igctl-scheduler /SC MINUTE /MO 5 /TR "igctl schedule run --once"[/dim]

[bold]cron[/bold] ([cyan]crontab -e[/cyan]):
[dim]*/5 * * * * igctl schedule run --once >> ~/.igctl/scheduler.log 2>&1[/dim]

[bold]systemd[/bold] (user unit [cyan]~/.config/systemd/user/igctl-scheduler.service[/cyan]):
[dim]\\[Service] ExecStart=igctl schedule run --loop --interval 60  Restart=on-failure[/dim]
then [cyan]systemctl --user enable --now igctl-scheduler[/cyan].

Use the full path to igctl (or `uv run --project <repo> igctl`) in these entries.
Several runners can share one queue safely: posts are claimed atomically.
"""


def _parse_when(text: str, tz: str | None) -> datetime:
    run_at = parse_when(text, tz)
    if run_at < datetime.now(UTC).replace(microsecond=0):
        warn(f"{format_local(run_at, tz)} is in the past; it will publish on the next run.")
    return run_at


def _row_summary(row: Any, tz: str | None = None) -> dict[str, Any]:
    req = queue.post_view(row)["request"] or {}
    files = req.get("paths") or req.get("urls") or []
    return {
        "id": row["id"],
        "account": row["account"],
        "status": row["status"],
        "run_at": format_local(row["run_at"], tz),
        "type": req.get("media_type"),
        "files": ", ".join(Path(f).name if "://" not in f else f for f in files),
        "caption": (req.get("caption") or "").replace("\n", " "),
        "attempts": row["attempts"],
        "error": row["error"],
    }


def _account_name(ctx: Context, account: str | None) -> str:
    return ctx.config.account(account or ctx.account_name).name


def check_hostable(
    ctx: Context, account: str, req: PublishRequest, backend: Backend | None = None
) -> None:
    """Reject posts the account's backend could never publish (Graph + local files and no
    ``media_host``) now, instead of failing at publish time, maybe hours later.

    ``backend`` is the instance about to publish; otherwise the account's configured one.
    """
    acct = ctx.config.account(account)
    name = backend.name if backend is not None else (ctx.backend_override or acct.backend)
    if name == "graph":
        check_graph_hostable(req, acct.graph_host, ctx.config)


def queue_post(
    ctx: Context, account: str, req: PublishRequest, when: str, tz: str | None = None
) -> int | None:
    """Validate, enqueue and report one post; shared by `schedule add` and `post --at`.

    ``--json`` prints the stored row (:func:`queue.post_view`, ISO-8601 UTC times).
    Returns the post id (None in dry-run).
    """
    check_hostable(ctx, account, req)
    run_at = _parse_when(when, tz)
    if ctx.dry_run:
        ok(
            f"[dry-run] would schedule a {req.media_type.value} for {account} "
            f"at {format_local(run_at, tz)}"
        )
        emit_ok(ctx, dry_run=True, account=account, run_at=run_at.isoformat())
        return None
    post_id = queue.enqueue(ctx.store, account, req, run_at)
    row = queue.get(ctx.store, post_id)
    if ctx.json_output:
        emit(ctx, queue.post_view(row))
    else:
        emit(ctx, _row_summary(row, tz), title="Scheduled post")
    ok(f"Scheduled #{post_id} for {format_local(run_at, tz)} (account {account!r}).")
    return post_id


@app.command("add")
def add(
    ctx: typer.Context,
    files: Annotated[list[str], typer.Argument(help="Media files (or public URLs) to post.")],
    at: Annotated[str, typer.Option("--at", help=WHEN_HELP)],
    media_type: Annotated[
        MediaType | None,
        typer.Option("--type", help="Default: carousel for 2+ files, reel for video."),
    ] = None,
    caption: Annotated[str, typer.Option("--caption", "-c", help="Post caption.")] = "",
    caption_file: Annotated[
        Path | None, typer.Option("--caption-file", help="Read the caption from a UTF-8 file.")
    ] = None,
    tz: Annotated[str | None, typer.Option("--tz", help=TZ_HELP)] = None,
    account: Annotated[str | None, typer.Option("--account", "-a", help="Account alias.")] = None,
    cover: Annotated[Path | None, typer.Option("--cover", help="Reel/video cover image.")] = None,
    location: Annotated[str | None, typer.Option("--location", help="Location name.")] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Tag a user (repeatable).")
    ] = None,
    no_feed: Annotated[
        bool, typer.Option("--no-feed", help="Reels: don't also share to the feed.")
    ] = False,
) -> None:
    """Queue a post to be published later."""
    c = get_ctx(ctx)
    if caption_file:
        if caption:
            raise IgctlError("Use --caption or --caption-file, not both.")
        caption = read_text_file(caption_file, "caption file").strip()
    name = _account_name(c, account)
    warnings: list[str] = []
    req = queue.build_request(
        files,
        media_type,
        caption,
        cover=cover,
        location=location,
        user_tags=tag or [],
        share_to_feed=not no_feed,
        warnings=warnings,
    )
    for w in warnings:
        warn(w)
    queue_post(c, name, req, at, tz)


@app.command("import")
def import_(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="Content calendar (.csv, .yaml or .yml).")],
    tz: Annotated[str | None, typer.Option("--tz", help=TZ_HELP)] = None,
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Account for rows without one.")
    ] = None,
) -> None:
    """Bulk-schedule posts from a content calendar.

    Columns/keys: [cyan]when[/cyan], [cyan]files[/cyan] (';'-separated), and optional
    [cyan]type[/cyan], [cyan]caption[/cyan], [cyan]account[/cyan], [cyan]tz[/cyan],
    [cyan]cover[/cyan], [cyan]location[/cyan]. Relative paths resolve next to the calendar.
    Every row is validated first; if any is invalid nothing is imported.
    """
    c = get_ctx(ctx)
    default = account or c.account_name or c.config.default_account
    results = queue.import_calendar(
        c.store,
        file,
        default_account=default,
        tz=tz,
        known_accounts=c.config.accounts.keys(),
        dry_run=c.dry_run,
        check=lambda acct, req: check_hostable(c, acct, req),
    )
    rows = [
        {
            "id": post_id,
            "row": e.row,
            "account": e.account,
            "run_at": format_local(e.run_at, tz),
            "type": e.request.media_type.value,
            "files": len(e.request.paths) + len(e.request.urls),
            "caption": e.request.caption.replace("\n", " "),
        }
        for post_id, e in results
    ]
    emit(c, rows, title="Imported posts")
    verb = "Validated" if c.dry_run else "Scheduled"
    ok(f"{verb} {len(rows)} post(s) from {file.name}.")


@app.command("list")
def list_(
    ctx: typer.Context,
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="pending | running | done | failed | cancelled"),
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Only this account.")
    ] = None,
    tz: Annotated[str | None, typer.Option("--tz", help="Display time zone.")] = None,
) -> None:
    """List scheduled posts (soonest first)."""
    c = get_ctx(ctx)
    rows = queue.list_posts(c.store, status, account or c.account_name)
    if c.json_output:
        emit(c, [queue.post_view(r) for r in rows])
        return
    if not rows:
        warn("No scheduled posts" + (f" with status '{status}'." if status else "."))
        return
    emit(
        c,
        [_row_summary(r, tz) for r in rows],
        columns=["id", "account", "status", "run_at", "type", "files", "caption", "attempts"],
        title="Scheduled posts",
    )


@app.command("show")
def show(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(help="Scheduled post id.")],
    tz: Annotated[str | None, typer.Option("--tz", help="Display time zone.")] = None,
) -> None:
    """Show one scheduled post in full, including its result or last error."""
    c = get_ctx(ctx)
    row = queue.get(c.store, post_id)
    view = queue.post_view(row)
    if not c.json_output:
        view["run_at"] = format_local(row["run_at"], tz)
        view["created_at"] = format_local(row["created_at"], tz)
    emit(c, view, title=f"Scheduled post #{post_id}")


@app.command("cancel")
def cancel(
    ctx: typer.Context, post_id: Annotated[int, typer.Argument(help="Scheduled post id.")]
) -> None:
    """Cancel a pending (or failed) post."""
    c = get_ctx(ctx)
    if c.dry_run:
        queue.get(c.store, post_id)
        ok(f"[dry-run] would cancel #{post_id}.")
        emit_ok(c, id=post_id, status="cancelled", dry_run=True)
        return
    queue.cancel(c.store, post_id)
    ok(f"Cancelled #{post_id}.")
    emit_ok(c, id=post_id, status="cancelled")


@app.command("retry")
def retry(
    ctx: typer.Context,
    post_id: Annotated[int, typer.Argument(help="Scheduled post id.")],
    at: Annotated[str, typer.Option("--at", help=WHEN_HELP)] = "now",
    tz: Annotated[str | None, typer.Option("--tz", help=TZ_HELP)] = None,
) -> None:
    """Re-queue a failed or cancelled post (resets its attempt count)."""
    c = get_ctx(ctx)
    run_at = _parse_when(at, tz)
    if c.dry_run:
        queue.get(c.store, post_id)
        ok(f"[dry-run] would retry #{post_id} at {format_local(run_at, tz)}.")
        emit_ok(c, id=post_id, status="pending", run_at=run_at.isoformat(), dry_run=True)
        return
    queue.retry(c.store, post_id, run_at)
    ok(f"Re-queued #{post_id} for {format_local(run_at, tz)}.")
    emit_ok(c, id=post_id, status="pending", run_at=run_at.isoformat())


def _context_factory(root: Context):
    def make(account: str) -> AccountContext:
        sub = Context(account_name=account, backend_override=root.backend_override)
        sub.__dict__["store"] = root.store  # share one sqlite connection (cached_property slot)
        return sub

    return make


def _print_outcome(o: RunOutcome) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    detail = escape(str(o.detail or ""))
    match o.status:
        case "done":
            err_console.print(
                f"{stamp} [green]published[/green] #{o.post_id} ({o.account}) "
                f"{o.permalink or o.media_id}"
            )
        case "retry":
            when = format_local(o.next_run_at) if o.next_run_at else "later"
            err_console.print(f"{stamp} [yellow]retry[/yellow] #{o.post_id} at {when}: {detail}")
        case "failed":
            err_console.print(f"{stamp} [red]failed[/red] #{o.post_id} ({o.account}): {detail}")
        case _:
            err_console.print(f"{stamp} [dim]{o.status}[/dim] #{o.post_id}: {detail}")


@app.command("run", epilog=RUN_EPILOG)
def run(
    ctx: typer.Context,
    loop: Annotated[
        bool, typer.Option("--loop/--once", help="Poll forever, or publish what's due and exit.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", "-i", min=1, help="Seconds between polls (--loop).")
    ] = 60.0,
) -> None:
    """Publish due posts once (for cron/Task Scheduler) or as a polling daemon."""
    c = get_ctx(ctx)
    store = c.store
    if not c.dry_run and (stale := queue.recover_stale(store, STALE_AFTER)):
        warn(f"Recovered {len(stale)} post(s) stuck in 'running': {', '.join(map(str, stale))}.")
    factory = _context_factory(c)

    if not loop:
        outcomes = run_due(store, factory, dry_run=c.dry_run, on_outcome=_print_outcome)
        if c.json_output:
            emit(c, [o.__dict__ for o in outcomes])
        elif not outcomes:
            ok("Nothing due.")
        if any(o.status == "failed" for o in outcomes):
            raise typer.Exit(1)
        return

    if c.dry_run:
        raise IgctlError("--dry-run is only supported with --once.")
    ok(f"Scheduler running every {interval:g}s. Press Ctrl+C to stop.")
    with StopFlag() as stop:
        run_loop(
            store,
            factory,
            interval=interval,
            stop=stop,
            on_outcome=_print_outcome,
            on_error=lambda e: warn(f"scheduler error: {type(e).__name__}: {e}"),
        )
    ok("Scheduler stopped.")
