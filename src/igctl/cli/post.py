"""`igctl post`: publish, edit, delete and list media."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from igctl.backends.base import IgctlError
from igctl.cli._common import confirm_or_abort, emit, get_ctx, ok, warn
from igctl.cli.download import resolve_media_ref
from igctl.core.context import Context
from igctl.core.media_host import is_video
from igctl.core.models import MediaType, PublishRequest
from igctl.core.validation import (  # noqa: F401 - re-exported
    FEED_MAX_RATIO,
    FEED_MIN_RATIO,
    IMAGE_EXT,
    MAX_CAPTION,
    MAX_HASHTAGS,
    MAX_USER_TAGS,
    VIDEO_EXT,
    caption_problems,
    validate_request,
)
from igctl.scheduler.timeparse import TimeParseError, parse_when

app = typer.Typer(no_args_is_help=True)


def parse_at(value: str, now: datetime | None = None) -> datetime:
    """``--at`` value (ISO-8601, ``+2h``, ``tomorrow 9am``...) to aware UTC; naive is local."""
    now = now or datetime.now(UTC)
    try:
        dt = parse_when(value, now=now)
    except TimeParseError as e:
        raise IgctlError(f"Invalid --at '{value}': {e}") from e
    if dt <= now:
        raise IgctlError(f"--at {value} is in the past.")
    return dt


def _read_caption(caption: str | None, caption_file: Path | None) -> str:
    if caption is not None and caption_file is not None:
        raise typer.BadParameter("use --caption or --caption-file, not both")
    if caption_file is not None:
        if str(caption_file) == "-":
            return sys.stdin.read().strip()
        try:
            return caption_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise IgctlError(f"Can't read caption file: {e}") from e
    return caption or ""


def _publish(
    ctx: typer.Context,
    media_type: MediaType,
    files: list[Path] | None,
    urls: list[str] | None,
    caption: str | None,
    caption_file: Path | None,
    location: str | None,
    tags: list[str] | None,
    at: str | None,
    yes: bool,
    cover: Path | None = None,
    share_to_feed: bool = True,
) -> None:
    c = get_ctx(ctx)
    req = PublishRequest(
        media_type=media_type,
        paths=[p.expanduser().resolve() for p in files or []],
        urls=list(urls or []),
        caption=_read_caption(caption, caption_file),
        cover=cover.expanduser().resolve() if cover else None,
        location=location,
        user_tags=[t.lstrip("@") for t in tags or []],
        share_to_feed=share_to_feed,
    )
    for w in validate_request(req):
        warn(w)
    what = f"{media_type} ({len(req.paths) or len(req.urls)} item(s))"

    if at:
        run_at = parse_at(at)
        if c.dry_run:
            ok(f"(dry-run) would schedule {what} for {run_at.isoformat()}")
            return
        post_id = c.store.enqueue_post(c.account.name, req.model_dump_json(), run_at)
        ok(f"Scheduled {what} as #{post_id} for {run_at.isoformat()} (account '{c.account.name}').")
        emit(c, {"id": post_id, "run_at": run_at.isoformat(), "account": c.account.name})
        return

    _publish_now(c, req, what, yes)


def _publish_now(c: Context, req: PublishRequest, what: str, yes: bool) -> None:
    be = c.backend()
    # graph.facebook.com accepts direct (resumable) uploads for videos; everything else
    # must be fetched by Instagram from a public URL.
    direct_upload = getattr(be, "is_facebook_host", False) and all(map(is_video, req.paths))
    if be.name == "graph" and req.paths and not c.config.media_host and not direct_upload:
        raise IgctlError(
            "The Graph API fetches media from public URLs. Pass --url, set `media_host` in "
            "config.yaml, or use --backend private."
        )
    confirm_or_abort(c, f"Publish {what} as account '{c.account.name}'?", yes)
    target = str(req.paths[0].name if req.paths else req.urls[0])
    result = c.guard.act("publish", target, lambda: be.publish(req))
    if result is None:
        ok(f"(dry-run) would publish {what} via {be.name}")
        return
    ok(f"Published {what}: {result.permalink or result.media_id}")
    emit(c, result)


# Shared option declarations ---------------------------------------------------
_URL = typer.Option(None, "--url", help="Public media URL instead of a file (repeatable).")
_CAPTION = typer.Option(None, "--caption", "-c", help="Caption text.")
_CAPTION_FILE = typer.Option(None, "--caption-file", help="Read caption from file ('-' = stdin).")
_LOCATION = typer.Option(None, "--location", help="Location name or id.")
_TAG = typer.Option(None, "--tag", "-t", help="Tag a user (repeatable).")
_AT = typer.Option(
    None, "--at", help="Schedule instead of posting now: ISO-8601, '+2h', 'tomorrow 9am'..."
)
_YES = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation.")


@app.command()
def photo(
    ctx: typer.Context,
    file: Annotated[Path | None, typer.Argument(help="Image file.")] = None,
    url: list[str] | None = _URL,
    caption: str | None = _CAPTION,
    caption_file: Path | None = _CAPTION_FILE,
    location: str | None = _LOCATION,
    tag: list[str] | None = _TAG,
    at: str | None = _AT,
    yes: bool = _YES,
) -> None:
    """Publish a single photo to the feed."""
    files = [file] if file else []
    _publish(ctx, MediaType.IMAGE, files, url, caption, caption_file, location, tag, at, yes)


@app.command()
def video(
    ctx: typer.Context,
    file: Annotated[Path | None, typer.Argument(help="Video file (mp4/mov).")] = None,
    url: list[str] | None = _URL,
    caption: str | None = _CAPTION,
    caption_file: Path | None = _CAPTION_FILE,
    location: str | None = _LOCATION,
    tag: list[str] | None = _TAG,
    at: str | None = _AT,
    yes: bool = _YES,
) -> None:
    """Publish a single feed video."""
    files = [file] if file else []
    _publish(ctx, MediaType.VIDEO, files, url, caption, caption_file, location, tag, at, yes)


@app.command()
def reel(
    ctx: typer.Context,
    file: Annotated[Path | None, typer.Argument(help="Video file (mp4/mov, ideally 9:16).")] = None,
    cover: Annotated[Path | None, typer.Option("--cover", help="Cover image.")] = None,
    no_feed: bool = typer.Option(False, "--no-feed", help="Only show in the Reels tab."),
    url: list[str] | None = _URL,
    caption: str | None = _CAPTION,
    caption_file: Path | None = _CAPTION_FILE,
    location: str | None = _LOCATION,
    tag: list[str] | None = _TAG,
    at: str | None = _AT,
    yes: bool = _YES,
) -> None:
    """Publish a reel."""
    files = [file] if file else []
    _publish(
        ctx,
        MediaType.REEL,
        files,
        url,
        caption,
        caption_file,
        location,
        tag,
        at,
        yes,
        cover=cover,
        share_to_feed=not no_feed,
    )


@app.command()
def carousel(
    ctx: typer.Context,
    files: Annotated[
        list[Path] | None, typer.Argument(help="2-10 images/videos, in order.")
    ] = None,
    url: list[str] | None = _URL,
    caption: str | None = _CAPTION,
    caption_file: Path | None = _CAPTION_FILE,
    location: str | None = _LOCATION,
    tag: list[str] | None = _TAG,
    at: str | None = _AT,
    yes: bool = _YES,
) -> None:
    """Publish a carousel (album) of 2-10 items."""
    _publish(ctx, MediaType.CAROUSEL, files, url, caption, caption_file, location, tag, at, yes)


@app.command()
def story(
    ctx: typer.Context,
    file: Annotated[Path | None, typer.Argument(help="Image or video, ideally 9:16.")] = None,
    url: list[str] | None = _URL,
    caption: str | None = _CAPTION,
    caption_file: Path | None = _CAPTION_FILE,
    location: str | None = _LOCATION,
    tag: list[str] | None = _TAG,
    at: str | None = _AT,
    yes: bool = _YES,
) -> None:
    """Publish a story."""
    files = [file] if file else []
    _publish(ctx, MediaType.STORY, files, url, caption, caption_file, location, tag, at, yes)


@app.command()
def delete(
    ctx: typer.Context,
    media: str = typer.Argument(..., metavar="MEDIA_ID", help="Media id, shortcode or URL."),
    yes: bool = _YES,
) -> None:
    """Delete one of your posts."""
    c = get_ctx(ctx)
    be = c.backend()
    media_id = resolve_media_ref(be, media)
    confirm_or_abort(
        c, f"Delete media {media_id} from '{c.account.name}'? This can't be undone", yes
    )

    def run() -> bool:
        be.delete_media(media_id)
        return True

    if c.guard.act("delete", media_id, run) is None:
        ok(f"(dry-run) would delete {media_id}")
        return
    ok(f"Deleted {media_id}")


@app.command()
def edit(
    ctx: typer.Context,
    media: str = typer.Argument(..., metavar="MEDIA_ID", help="Media id, shortcode or URL."),
    caption: str | None = _CAPTION,
    caption_file: Path | None = _CAPTION_FILE,
) -> None:
    """Replace a post's caption."""
    c = get_ctx(ctx)
    if caption is None and caption_file is None:
        raise typer.BadParameter("give --caption or --caption-file")
    text = _read_caption(caption, caption_file)
    if problem := caption_problems(text):
        raise IgctlError(problem)
    be = c.backend()
    media_id = resolve_media_ref(be, media)

    def run() -> bool:
        be.edit_caption(media_id, text)
        return True

    if c.guard.act("edit", media_id, run) is None:
        ok(f"(dry-run) would update the caption of {media_id}")
        return
    ok(f"Updated caption of {media_id}")


@app.command("list")
def list_posts(
    ctx: typer.Context,
    username: str | None = typer.Argument(None, help="Defaults to the active account."),
    limit: int = typer.Option(20, "--limit", "-n", min=1),
) -> None:
    """List recent posts."""
    c = get_ctx(ctx)
    media = c.backend().list_media(username.lstrip("@") if username else None, limit=limit)
    emit(
        c,
        media,
        columns=[
            "id",
            "code",
            "media_type",
            "taken_at",
            "like_count",
            "comment_count",
            "caption",
        ],
        title=f"@{username}" if username else "Recent posts",
    )
