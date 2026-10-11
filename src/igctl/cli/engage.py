"""`igctl engage`: likes, comments, follows, moderation and bulk engagement.

Every write goes through :meth:`Guard.act`, so it is paced, capped per 24h, logged and
dry-run aware. Bulk commands stop cleanly when a cap (or Instagram) rate-limits them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from igctl.backends.base import AuthError, Backend, NotSupported, RateLimited
from igctl.cli._common import confirm_or_abort, emit, err_console, get_ctx, ok, warn
from igctl.core.context import Context
from igctl.core.refs import (  # noqa: F401 - re-exported for other CLI modules
    normalize_username,
    pk_to_shortcode,
    resolve_media,
    shortcode_from_url,
    shortcode_to_pk,
)

app = typer.Typer(no_args_is_help=True)

MEDIA_COLUMNS = ["id", "code", "media_type", "owner_username", "like_count", "comment_count"]
USER_COLUMNS = ["id", "username", "full_name", "is_private", "is_verified"]
COMMENT_COLUMNS = ["id", "username", "text", "created_at", "like_count"]

# --------------------------------------------------------------------------------------
# Media / user reference resolution
# --------------------------------------------------------------------------------------


def read_lines(path: Path, usernames: bool = True) -> list[str]:
    """Non-empty, non-comment lines of ``path``, de-duplicated in order."""
    seen: dict[str, None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        value = normalize_username(line) if usernames else line
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _media_or_bad(backend: Backend, ref: str) -> str:
    try:
        return resolve_media(backend, ref)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e


# --------------------------------------------------------------------------------------
# Guarded execution
# --------------------------------------------------------------------------------------


@dataclass
class BulkResult:
    action: str
    total: int
    done: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = False
    stopped: str | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def run_bulk(
    c: Context,
    action: str,
    items: Sequence[tuple[str, Callable[[], Any]]],
    *,
    label: str | None = None,
    progress: bool = True,
    on_success: Callable[[str], None] | None = None,
) -> BulkResult:
    """Run ``(target, fn)`` pairs through the guard as ``action``.

    Stops at the first :class:`RateLimited`; :class:`NotSupported` / :class:`AuthError`
    abort (they would fail for every item); other errors are recorded and skipped.
    ``on_success`` is called with the target after each real (non dry-run) success.
    """
    res = BulkResult(action=action, total=len(items), dry_run=c.dry_run)
    bar = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[target]}", style="dim"),
        TimeElapsedColumn(),
        console=err_console,
        disable=not progress,
    )
    with bar:
        task = bar.add_task(label or action, total=len(items), target="")
        for target, fn in items:
            bar.update(task, target=target)
            try:
                c.guard.act(action, target, fn)
            except RateLimited as e:
                res.stopped = str(e)
                break
            except (NotSupported, AuthError):
                raise
            except Exception as e:  # one bad item must not kill the batch
                res.failed += 1
                res.errors.append({"target": target, "error": str(e)})
            else:
                res.done += 1
                if on_success and not c.dry_run:
                    on_success(target)
            bar.advance(task)
    return res


def report_bulk(c: Context, res: BulkResult, title: str) -> None:
    for err in res.errors:
        warn(f"{err['target']}: {err['error']}")
    if res.stopped:
        warn(f"Stopped early: {res.stopped}")
    data: dict[str, Any] = asdict(res)
    if not c.json_output:
        data.pop("errors")
    emit(c, data, title=title)


def _each(
    c: Context, action: str, targets: Iterable[str], fn: Callable[[str], Any], verb: str
) -> None:
    """Apply a guarded single action to one or more targets (non-bulk commands)."""
    targets = list(targets)
    items = [(t, (lambda t=t: fn(t))) for t in targets]
    res = run_bulk(c, action, items, progress=len(items) > 1)
    for err in res.errors:
        warn(f"{err['target']}: {err['error']}")
    if res.stopped:
        warn(f"Stopped: {res.stopped}")
    if res.done:
        prefix = "[dry-run] " if c.dry_run else ""
        ok(f"{prefix}{verb} {res.done}/{len(targets)}")
    if c.json_output:
        emit(c, asdict(res))
    if res.failed or res.stopped:
        raise typer.Exit(1)


def _single(c: Context, action: str, target: str, fn: Callable[[], Any], msg: str) -> Any:
    result = c.guard.act(action, target, fn)
    if c.dry_run:
        warn(f"[dry-run] would {msg}")
    else:
        ok(msg)
    return result


def _take(it: Iterable[Any], limit: int) -> list[Any]:
    return list(islice(it, limit) if limit > 0 else it)


# --------------------------------------------------------------------------------------
# Likes
# --------------------------------------------------------------------------------------

MediaArgs = Annotated[list[str], typer.Argument(help="Media ids, shortcodes or post URLs.")]


@app.command()
def like(ctx: typer.Context, media: MediaArgs) -> None:
    """Like one or more posts."""
    c = get_ctx(ctx)
    b = c.backend()
    ids = [_media_or_bad(b, m) for m in media]
    _each(c, "like", ids, b.like, "liked")


@app.command()
def unlike(ctx: typer.Context, media: MediaArgs) -> None:
    """Remove your like from one or more posts."""
    c = get_ctx(ctx)
    b = c.backend()
    ids = [_media_or_bad(b, m) for m in media]
    _each(c, "unlike", ids, b.unlike, "unliked")


# --------------------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------------------


@app.command()
def comment(
    ctx: typer.Context,
    media: Annotated[str, typer.Argument(help="Media id, shortcode or URL.")],
    text: Annotated[str, typer.Argument(help="Comment text.")],
    reply_to: Annotated[
        str | None, typer.Option("--reply-to", help="Comment id to reply to.")
    ] = None,
) -> None:
    """Comment on a post (or reply to a comment)."""
    c = get_ctx(ctx)
    b = c.backend()
    mid = _media_or_bad(b, media)
    result = _single(
        c, "comment", mid, lambda: b.comment(mid, text, reply_to=reply_to), f"commented on {mid}"
    )
    if result is not None and c.json_output:
        emit(c, result)


@app.command("comments")
def list_comments(
    ctx: typer.Context,
    media: Annotated[str, typer.Argument(help="Media id, shortcode or URL.")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max comments.")] = 50,
) -> None:
    """List comments on a post."""
    c = get_ctx(ctx)
    b = c.backend()
    mid = _media_or_bad(b, media)
    emit(c, b.list_comments(mid, limit=limit), COMMENT_COLUMNS, title=f"Comments on {mid}")


@app.command("delete-comment")
def delete_comment(
    ctx: typer.Context,
    media: Annotated[str, typer.Argument(help="Media id, shortcode or URL.")],
    comment_id: Annotated[str, typer.Argument(metavar="COMMENT", help="Comment id.")],
) -> None:
    """Delete a comment."""
    c = get_ctx(ctx)
    b = c.backend()
    mid = _media_or_bad(b, media)
    _single(
        c,
        "delete_comment",
        comment_id,
        lambda: b.delete_comment(mid, comment_id),
        f"deleted comment {comment_id}",
    )


@app.command("hide-comment")
def hide_comment(
    ctx: typer.Context,
    comment_id: Annotated[str, typer.Argument(metavar="COMMENT", help="Comment id.")],
    unhide: Annotated[bool, typer.Option("--unhide", help="Unhide instead.")] = False,
) -> None:
    """Hide (or unhide) a comment."""
    c = get_ctx(ctx)
    b = c.backend()
    verb = "unhid" if unhide else "hid"
    _single(
        c,
        "hide_comment",
        comment_id,
        lambda: b.hide_comment(comment_id, hide=not unhide),
        f"{verb} comment {comment_id}",
    )


@app.command("purge-comments")
def purge_comments(
    ctx: typer.Context,
    media: Annotated[str, typer.Argument(help="Media id, shortcode or URL.")],
    match: Annotated[str, typer.Option("--match", "-m", help="Regex searched in comment text.")],
    hide: Annotated[bool, typer.Option("--hide", help="Hide matches instead of deleting.")] = False,
    ignore_case: Annotated[bool, typer.Option("--ignore-case", "-i")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Max comments scanned.")] = 500,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete (or hide) every comment on a post whose text matches a regex."""
    c = get_ctx(ctx)
    try:
        pattern = re.compile(match, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise typer.BadParameter(f"invalid regex: {e}", param_hint="--match") from e
    b = c.backend()
    mid = _media_or_bad(b, media)
    hits = [cm for cm in b.list_comments(mid, limit=limit) if pattern.search(cm.text)]
    if not hits:
        ok("No matching comments.")
        return
    if not c.json_output:
        emit(c, hits, COMMENT_COLUMNS, title=f"{len(hits)} matching comments")
    verb = "Hide" if hide else "Delete"
    confirm_or_abort(c, f"{verb} {len(hits)} comment(s)?", yes)
    if hide:
        action = "hide_comment"
        items = [(cm.id, (lambda cid=cm.id: b.hide_comment(cid, hide=True))) for cm in hits]
    else:
        action = "delete_comment"
        items = [(cm.id, (lambda cid=cm.id: b.delete_comment(mid, cid))) for cm in hits]
    report_bulk(c, run_bulk(c, action, items, label=f"{verb.lower()} comments"), "purge-comments")


# --------------------------------------------------------------------------------------
# Follows
# --------------------------------------------------------------------------------------

UserArgs = Annotated[list[str], typer.Argument(help="Usernames (@name or profile URL ok).")]


@app.command()
def follow(ctx: typer.Context, users: UserArgs) -> None:
    """Follow one or more users."""
    c = get_ctx(ctx)
    b = c.backend()
    _each(c, "follow", [normalize_username(u) for u in users], b.follow, "followed")


@app.command()
def unfollow(ctx: typer.Context, users: UserArgs) -> None:
    """Unfollow one or more users."""
    c = get_ctx(ctx)
    b = c.backend()
    _each(c, "unfollow", [normalize_username(u) for u in users], b.unfollow, "unfollowed")


OptUser = Annotated[str | None, typer.Argument(help="Username (default: you).")]
Limit = Annotated[int, typer.Option("--limit", "-n", help="Max results (0 = all).")]


@app.command()
def followers(ctx: typer.Context, user: OptUser = None, limit: Limit = 50) -> None:
    """List followers of a user."""
    c = get_ctx(ctx)
    name = normalize_username(user) if user else None
    rows = _take(c.backend().followers(name, limit=limit), limit)
    emit(c, rows, USER_COLUMNS, title=f"Followers of {name or 'me'} ({len(rows)})")


@app.command()
def following(ctx: typer.Context, user: OptUser = None, limit: Limit = 50) -> None:
    """List accounts a user follows."""
    c = get_ctx(ctx)
    name = normalize_username(user) if user else None
    rows = _take(c.backend().following(name, limit=limit), limit)
    emit(c, rows, USER_COLUMNS, title=f"Followed by {name or 'me'} ({len(rows)})")


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


@app.command()
def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search text.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """Search users."""
    c = get_ctx(ctx)
    emit(c, c.backend().search_users(query, limit=limit), USER_COLUMNS, title=f"Users: {query}")


@app.command()
def hashtag(
    ctx: typer.Context,
    tag: Annotated[str, typer.Argument(help="Hashtag, with or without #.")],
    recent: Annotated[bool, typer.Option("--recent", help="Recent instead of top media.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
) -> None:
    """List top (or recent) media for a hashtag."""
    c = get_ctx(ctx)
    tag = tag.lstrip("#")
    media = c.backend().hashtag_media(tag, limit=limit, recent=recent)
    emit(c, media, MEDIA_COLUMNS, title=f"#{tag} ({'recent' if recent else 'top'})")


# --------------------------------------------------------------------------------------
# Bulk
# --------------------------------------------------------------------------------------

Max = Annotated[int, typer.Option("--max", help="Maximum number of actions.", min=1)]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")]


@app.command("bulk-like")
def bulk_like(
    ctx: typer.Context,
    tag: Annotated[str, typer.Option("--hashtag", "-t", help="Hashtag to like posts from.")],
    max_actions: Max = 20,
    recent: Annotated[bool, typer.Option("--recent/--top", help="Recent or top media.")] = True,
) -> None:
    """Like up to --max posts from a hashtag."""
    c = get_ctx(ctx)
    b = c.backend()
    tag = tag.lstrip("#")
    with err_console.status(f"Fetching #{tag}..."):
        media = b.hashtag_media(tag, limit=max_actions, recent=recent)[:max_actions]
    items = [(m.id, (lambda mid=m.id: b.like(mid))) for m in media]
    report_bulk(c, run_bulk(c, "like", items, label=f"like #{tag}"), "bulk-like")


@app.command("bulk-follow")
def bulk_follow(
    ctx: typer.Context,
    from_file: Annotated[
        Path,
        typer.Option("--from-file", "-f", exists=True, dir_okay=False, help="One user per line."),
    ],
    max_actions: Max = 20,
) -> None:
    """Follow up to --max users listed in a file."""
    c = get_ctx(ctx)
    b = c.backend()
    users = read_lines(from_file)[:max_actions]
    items = [(u, (lambda u=u: b.follow(u))) for u in users]
    report_bulk(c, run_bulk(c, "follow", items, label="follow"), "bulk-follow")


@app.command("bulk-unfollow")
def bulk_unfollow(
    ctx: typer.Context,
    non_followers: Annotated[
        bool,
        typer.Option(
            "--non-followers/--all-following",
            help="Only accounts that don't follow you back (default), or everyone you follow.",
        ),
    ] = True,
    max_actions: Max = 20,
    whitelist: Annotated[
        Path | None,
        typer.Option("--whitelist", "-w", exists=True, dir_okay=False, help="Never unfollow."),
    ] = None,
    yes: Yes = False,
) -> None:
    """Unfollow up to --max accounts (by default those not following you back)."""
    c = get_ctx(ctx)
    b = c.backend()
    keep = {u.lower() for u in read_lines(whitelist)} if whitelist else set()
    with err_console.status("Fetching following..."):
        follows = list(b.following(None, limit=0))
    follower_ids: set[str] = set()
    if non_followers:
        with err_console.status("Fetching followers..."):
            follower_ids = {u.id for u in b.followers(None, limit=0)}
    seen: set[str] = set()
    targets: list[str] = []
    for u in follows:
        if u.id in seen or u.id in follower_ids or u.username.lower() in keep:
            continue
        seen.add(u.id)
        targets.append(u.username)
    targets = targets[:max_actions]
    if not targets:
        ok("Nothing to unfollow.")
        return
    preview = ", ".join(targets[:10]) + (" ..." if len(targets) > 10 else "")
    confirm_or_abort(c, f"Unfollow {len(targets)} account(s)? ({preview})", yes)
    items = [(u, (lambda u=u: b.unfollow(u))) for u in targets]
    report_bulk(c, run_bulk(c, "unfollow", items, label="unfollow"), "bulk-unfollow")
