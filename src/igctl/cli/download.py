"""`igctl download`: save posts, whole profiles and stories to disk."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.progress import Progress

from igctl.backends.base import Backend, IgctlError, NotSupported
from igctl.cli._common import emit, get_ctx, ok, warn
from igctl.core.refs import media_ref_to_pk, resolve_media

app = typer.Typer(no_args_is_help=True)


def media_ref_to_id(ref: str) -> str:
    """Accept a media id, ``pk_userid`` id, shortcode or instagram.com post/reel URL."""
    try:
        return media_ref_to_pk(ref)
    except ValueError as e:
        raise IgctlError(str(e)) from e


def resolve_media_ref(backend: Backend, ref: str) -> str:
    try:
        return resolve_media(backend, ref)
    except ValueError as e:
        raise IgctlError(str(e)) from e


def _dest(dest: Path) -> Path:
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    return dest


@app.command()
def post(
    ctx: typer.Context,
    ref: str = typer.Argument(..., metavar="URL_OR_ID", help="Post URL, shortcode or media id."),
    dest: Annotated[Path, typer.Option("--dest", "-d", help="Output directory.")] = Path("."),
) -> None:
    """Download one post (all carousel items)."""
    c = get_ctx(ctx)
    be = c.backend()
    files = be.download_media(resolve_media_ref(be, ref), str(_dest(dest)))
    ok(f"Downloaded {len(files)} file(s) to {dest}")
    emit(c, [{"file": f} for f in files])


@app.command()
def profile(
    ctx: typer.Context,
    username: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="Max posts to fetch."),
    dest: Annotated[Path | None, typer.Option("--dest", "-d", help="Default: ./<username>")] = None,
) -> None:
    """Download a profile's recent posts."""
    c = get_ctx(ctx)
    username = username.lstrip("@")
    be = c.backend()
    out = _dest(dest or Path(username))
    media = be.list_media(username, limit=limit)
    files: list[str] = []
    failed = 0
    with Progress(transient=True, disable=c.json_output) as progress:
        task = progress.add_task(f"@{username}", total=len(media))
        for m in media:
            try:
                files.extend(be.download_media(m.id, str(out)))
            except NotSupported:
                raise
            except Exception as e:  # keep going; one broken post shouldn't stop the batch
                failed += 1
                warn(f"{m.code or m.id}: {e}")
            progress.advance(task)
    ok(f"Downloaded {len(files)} file(s) from {len(media) - failed} post(s) to {out}")
    if c.json_output:
        emit(c, {"dest": str(out), "files": files, "failed": failed})
    if failed:
        raise typer.Exit(1)


@app.command()
def stories(
    ctx: typer.Context,
    username: str = typer.Argument(...),
    dest: Annotated[
        Path | None, typer.Option("--dest", "-d", help="Default: ./<username>/stories")
    ] = None,
) -> None:
    """Download a user's currently active stories."""
    c = get_ctx(ctx)
    username = username.lstrip("@")
    be = c.backend()
    out = _dest(dest or Path(username) / "stories")
    files = be.download_stories(username, str(out))
    ok(f"Downloaded {len(files)} story item(s) to {out}")
    emit(c, [{"file": f} for f in files])
