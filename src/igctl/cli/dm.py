"""`igctl dm`: read the inbox, send and reply to direct messages, and broadcast."""

from __future__ import annotations

import hashlib
import string
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import typer

from igctl.backends.base import Backend
from igctl.cli._common import confirm_or_abort, emit, emit_ok, get_ctx, ok, warn
from igctl.cli.engage import BulkResult, normalize_username, read_lines, report_bulk, run_bulk
from igctl.core.files import read_text_file
from igctl.core.models import DirectThread
from igctl.core.ratelimit import requires

app = typer.Typer(no_args_is_help=True)

TEMPLATE_FIELDS = {"username", "full_name", "first_name"}


def _thread_row(t: DirectThread) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title or ", ".join(u.username for u in t.users),
        "users": ", ".join(u.username for u in t.users),
        "last_message": t.last_message,
        "last_activity_at": t.last_activity_at,
        "unread": t.unread,
    }


@app.command()
def inbox(
    ctx: typer.Context,
    unread: Annotated[bool, typer.Option("--unread", help="Only unread threads.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", min=0, help="Max threads (0 = all).")] = 20,
) -> None:
    """List direct-message threads."""
    c = get_ctx(ctx)
    threads = c.backend().list_threads(limit=limit, unread_only=unread)
    data = threads if c.json_output else [_thread_row(t) for t in threads]
    emit(
        c,
        data,
        ["id", "title", "last_message", "last_activity_at", "unread"],
        title="Unread threads" if unread else "Inbox",
    )


@app.command()
def read(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=0, help="Max messages (0 = all).")
    ] = 20,
) -> None:
    """Show the latest messages of a thread."""
    c = get_ctx(ctx)
    messages = c.backend().thread_messages(thread, limit=limit)
    emit(c, messages, ["created_at", "user_id", "text"], title=f"Thread {thread}")


@app.command()
def send(
    ctx: typer.Context,
    users: Annotated[list[str], typer.Argument(help="Recipient usernames.")],
    text: Annotated[str | None, typer.Option("--text", "-t", help="Message text.")] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", exists=True, dir_okay=False, help="Read text from file."),
    ] = None,
    group: Annotated[
        bool, typer.Option("--group", help="One group thread instead of one DM per user.")
    ] = False,
) -> None:
    """Send a direct message to one or more users."""
    if (text is None) == (file is None):
        raise typer.BadParameter("pass exactly one of --text or --file")
    body = read_text_file(file, "message file").strip() if file is not None else (text or "")
    if not body:
        raise typer.BadParameter("message is empty")
    c = get_ctx(ctx)
    b = c.backend()
    names = [normalize_username(u) for u in users]
    batches = [names] if group else [[n] for n in names]
    items = [(",".join(r), partial(b.send_dm, r, body)) for r in batches]
    res = run_bulk(c, "dm", items, progress=len(items) > 1)
    for err in res.errors:
        warn(f"{err['target']}: {err['error']}")
    if res.stopped:
        warn(f"Stopped: {res.stopped}")
    if res.done:
        ok(f"{'[dry-run] would send' if c.dry_run else 'sent'} {res.done}/{len(items)} message(s)")
    if c.json_output:
        report_bulk(c, res, "dm send")
    if res.failed or res.stopped:
        raise typer.Exit(1)


@app.command()
def reply(
    ctx: typer.Context,
    thread: Annotated[str, typer.Argument(help="Thread id.")],
    text: Annotated[str, typer.Argument(help="Message text.")],
) -> None:
    """Reply in an existing thread."""
    c = get_ctx(ctx)
    b = c.backend()
    result = c.guard.act("dm", thread, partial(b.reply_dm, thread, text))
    if c.dry_run:
        warn(f"[dry-run] would reply in thread {thread}")
        emit_ok(c, action="dm", target=thread, dry_run=True)
        return
    ok(f"replied in thread {thread}")
    if c.json_output and result is not None:
        emit(c, result)


def template_fields(template: str) -> set[str]:
    """Placeholder names used in a ``str.format`` template."""
    try:
        return {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    except ValueError as e:
        raise typer.BadParameter(f"bad template: {e}", param_hint="--template") from e


def render(template: str, backend: Backend, username: str, fields: set[str]) -> str:
    values = {"username": username}
    if fields & {"full_name", "first_name"}:
        full = backend.get_profile(username).full_name or username
        values |= {"full_name": full, "first_name": full.split()[0] if full.split() else username}
    return template.format_map(values)


def broadcast_namespace(template: str) -> str:
    return "broadcast:" + hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]


@app.command()
def broadcast(
    ctx: typer.Context,
    users_file: Annotated[
        Path,
        typer.Option("--users-file", "-f", exists=True, dir_okay=False, help="One user per line."),
    ],
    template: Annotated[
        str,
        typer.Option(
            "--template", "-t", help="Message; placeholders: {username} {full_name} {first_name}."
        ),
    ],
    max_actions: Annotated[int, typer.Option("--max", min=1, help="Max messages this run.")] = 20,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """DM a templated message to each user in a file, once per user per template."""
    fields = template_fields(template)
    if unknown := fields - TEMPLATE_FIELDS:
        raise typer.BadParameter(
            f"unknown placeholder(s): {', '.join(sorted(unknown))}", param_hint="--template"
        )
    c = get_ctx(ctx)
    b = c.backend()
    acct, ns = c.account.name, broadcast_namespace(template)
    users = read_lines(users_file)
    pending = [u for u in users if not c.store.is_seen(acct, ns, u.lower())]
    skipped = len(users) - len(pending)
    pending = pending[:max_actions]
    if not pending:
        ok(f"Nothing to send ({skipped} already messaged with this template).")
        if c.json_output:
            emit(c, asdict(BulkResult(action="dm", total=0, skipped=skipped, dry_run=c.dry_run)))
        return
    if not c.json_output:
        ok(f"Preview: {template.format_map({f: f'<{f}>' for f in fields})}")
    confirm_or_abort(c, f"Send DM to {len(pending)} user(s)?", yes)
    items = [
        (u, requires(b, "send_dm", lambda u=u: b.send_dm([u], render(template, b, u, fields))))
        for u in pending
    ]
    res = run_bulk(
        c,
        "dm",
        items,
        label="broadcast",
        on_success=lambda u: c.store.mark_seen(acct, ns, u.lower()),
    )
    res.skipped = skipped
    report_bulk(c, res, "dm broadcast")
