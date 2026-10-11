"""Scheduled-post queue: validation, enqueueing, calendar import and state transitions.

Built on :class:`~igctl.core.storage.Store`'s ``scheduled_posts`` table. Transitions the
store does not provide (cancel, retry, reschedule, stale recovery) are implemented here with
raw SQL on the store's connection. Claim times live in a small side table owned by this
module so stale ``running`` rows can be detected reliably.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from igctl.backends.base import IgctlError
from igctl.core.models import MediaType, PublishRequest
from igctl.core.storage import Store, iso, utcnow
from igctl.core.validation import (
    CAROUSEL_MAX,
    CAROUSEL_MIN,
    IMAGE_EXT,
    VIDEO_EXT,
    caption_problems,
    validate_request,
)
from igctl.scheduler.timeparse import TimeParseError, parse_when

IMAGE_EXTS = IMAGE_EXT
VIDEO_EXTS = VIDEO_EXT

STATUSES = ("pending", "running", "done", "failed", "cancelled")

_CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_claims (
    post_id INTEGER PRIMARY KEY,
    claimed_at TEXT NOT NULL
);
"""


class ScheduleError(IgctlError):
    pass


class CalendarError(ScheduleError):
    """One or more calendar rows are invalid; ``errors`` lists them all."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} invalid row(s) in calendar; nothing was imported:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


# --- request building -------------------------------------------------------


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _kind(path_or_url: str) -> str | None:
    suffix = Path(path_or_url.split("?", 1)[0]).suffix.lower()
    if suffix in IMAGE_EXTS:
        return "image"
    if suffix in VIDEO_EXTS:
        return "video"
    return None


def infer_media_type(files: list[str]) -> MediaType:
    if len(files) > 1:
        return MediaType.CAROUSEL
    return MediaType.REEL if files and _kind(files[0]) == "video" else MediaType.IMAGE


def build_request(
    files: Iterable[str | Path],
    media_type: MediaType | str | None = None,
    caption: str = "",
    *,
    base_dir: Path | None = None,
    cover: str | Path | None = None,
    location: str | None = None,
    user_tags: Iterable[str] = (),
    share_to_feed: bool = True,
    warnings: list[str] | None = None,
) -> PublishRequest:
    """Validate inputs and build a :class:`PublishRequest` with absolute local paths.

    Entries starting with ``http(s)://`` become ``urls``; everything else must be an existing
    local file (relative paths resolve against ``base_dir``, default the CWD). Non-fatal
    issues (e.g. a story image that isn't 9:16) are appended to ``warnings`` if given.
    """
    base = (base_dir or Path.cwd()).resolve()
    raw = [str(f).strip() for f in files if str(f).strip()]
    if not raw:
        raise ScheduleError("At least one media file is required.")

    def _local(p: str | Path) -> Path:
        path = Path(p).expanduser()
        path = (path if path.is_absolute() else base / path).resolve()
        if not path.is_file():
            raise ScheduleError(f"File not found: {path}")
        return path

    paths = [_local(f) for f in raw if not _is_url(f)]
    urls = [f for f in raw if _is_url(f)]
    if paths and urls:
        raise ScheduleError("Use either local files or URLs in one post, not both.")
    try:
        mt = MediaType(media_type.lower()) if isinstance(media_type, str) else media_type
    except ValueError:
        choices = ", ".join(m.value for m in MediaType)
        raise ScheduleError(f"Unknown media type '{media_type}' (choose: {choices}).") from None
    mt = mt or infer_media_type(raw)

    kinds = [_kind(f) for f in raw]
    if None in kinds:
        bad = raw[kinds.index(None)]
        raise ScheduleError(f"Unsupported media file '{bad}' (use jpg/png/webp/mp4/mov).")
    if mt == MediaType.CAROUSEL:
        if not CAROUSEL_MIN <= len(raw) <= CAROUSEL_MAX:
            raise ScheduleError(
                f"A carousel needs {CAROUSEL_MIN}-{CAROUSEL_MAX} items, got {len(raw)}."
            )
    elif len(raw) != 1:
        raise ScheduleError(f"A {mt.value} takes exactly one file, got {len(raw)}.")
    elif mt == MediaType.IMAGE and kinds[0] != "image":
        raise ScheduleError("An image post needs an image file (use --type reel for video).")
    elif mt in (MediaType.VIDEO, MediaType.REEL) and kinds[0] != "video":
        raise ScheduleError(f"A {mt.value} needs a video file (mp4/mov).")

    if problem := caption_problems(caption):
        raise ScheduleError(problem)

    req = PublishRequest(
        media_type=mt,
        paths=paths,
        urls=urls,
        caption=caption,
        cover=_local(cover) if cover else None,
        location=location or None,
        user_tags=[t.lstrip("@") for t in user_tags if t.strip()],
        share_to_feed=share_to_feed,
    )
    try:  # aspect ratio, cover and tag checks shared with `igctl post`
        found = validate_request(req)
    except ScheduleError:
        raise
    except IgctlError as e:
        raise ScheduleError(str(e)) from e
    if warnings is not None:
        warnings.extend(found)
    return req


# --- queue operations -------------------------------------------------------


def enqueue(store: Store, account: str, request: PublishRequest, run_at: datetime) -> int:
    """Queue ``request`` for ``account`` at ``run_at`` (aware datetime). Returns the post id."""
    if run_at.tzinfo is None:
        raise ScheduleError("run_at must be timezone-aware.")
    return store.enqueue_post(account, request.model_dump_json(), run_at)


def get(store: Store, post_id: int) -> sqlite3.Row:
    row = store.get_post(post_id)
    if row is None:
        raise ScheduleError(f"No scheduled post with id {post_id}.")
    return row


def cancel(store: Store, post_id: int) -> None:
    """Cancel a pending (or failed) post. Running/done posts cannot be cancelled."""
    row = get(store, post_id)
    with store.tx() as c:
        cur = c.execute(
            "UPDATE scheduled_posts SET status='cancelled' "
            "WHERE id=? AND status IN ('pending', 'failed')",
            (post_id,),
        )
    if cur.rowcount != 1:
        raise ScheduleError(f"Post {post_id} is '{row['status']}' and can't be cancelled.")


def retry(store: Store, post_id: int, run_at: datetime | None = None) -> None:
    """Re-queue a failed or cancelled post with a fresh attempt budget."""
    row = get(store, post_id)
    with store.tx() as c:
        cur = c.execute(
            "UPDATE scheduled_posts SET status='pending', attempts=0, error=NULL, result=NULL, "
            "run_at=? WHERE id=? AND status IN ('failed', 'cancelled')",
            (iso(run_at), post_id),
        )
    if cur.rowcount != 1:
        raise ScheduleError(
            f"Post {post_id} is '{row['status']}'; only failed or cancelled posts can be retried."
        )


def reschedule(
    store: Store,
    post_id: int,
    run_at: datetime,
    error: str | None = None,
    *,
    refund_attempt: bool = False,
) -> None:
    """Put a post back to pending at ``run_at``.

    ``refund_attempt`` undoes the attempt counted by the claim (used when nothing was tried).
    """
    with store.tx() as c:
        c.execute(
            "UPDATE scheduled_posts SET status='pending', run_at=?, error=?, "
            "attempts=MAX(0, attempts - ?) WHERE id=?",
            (iso(run_at), error, int(refund_attempt), post_id),
        )


def _ensure_claims(store: Store) -> None:
    store._conn.executescript(_CLAIMS_SCHEMA)


def record_claim(store: Store, post_id: int, at: datetime | None = None) -> None:
    _ensure_claims(store)
    with store.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO scheduler_claims(post_id, claimed_at) VALUES (?, ?)",
            (post_id, iso(at)),
        )


def recover_stale(
    store: Store,
    older_than: timedelta = timedelta(hours=1),
    max_attempts: int = 3,
    now: datetime | None = None,
) -> list[int]:
    """Reset posts stuck in ``running`` (crashed runner) for longer than ``older_than``.

    They go back to ``pending`` (or ``failed`` once the attempt budget is spent). The
    publish may have partially succeeded before the crash, so the error notes a possible
    duplicate. Returns the affected ids.
    """
    _ensure_claims(store)
    cutoff = iso((now or utcnow()) - older_than)
    rows = store._conn.execute(
        "SELECT p.id, p.attempts FROM scheduled_posts p "
        "LEFT JOIN scheduler_claims c ON c.post_id = p.id "
        "WHERE p.status='running' AND COALESCE(c.claimed_at, p.run_at) < ?",
        (cutoff,),
    ).fetchall()
    note = "Recovered from a stale 'running' state (runner interrupted); check for a duplicate."
    with store.tx() as c:
        for r in rows:
            status = "pending" if r["attempts"] < max_attempts else "failed"
            c.execute(
                "UPDATE scheduled_posts SET status=?, error=? WHERE id=? AND status='running'",
                (status, note, r["id"]),
            )
    return [r["id"] for r in rows]


def list_posts(
    store: Store, status: str | None = None, account: str | None = None
) -> list[sqlite3.Row]:
    if status and status not in STATUSES:
        raise ScheduleError(f"Unknown status '{status}' (choose: {', '.join(STATUSES)}).")
    rows = store.list_posts(status)
    return [r for r in rows if account is None or r["account"] == account]


def post_view(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    """A plain dict for display: the row with its request/result JSON decoded."""
    data = dict(row)
    data["request"] = json.loads(data["request"]) if data.get("request") else None
    if data.get("result"):
        try:
            data["result"] = json.loads(data["result"])
        except ValueError:
            pass
    return data


# --- content calendars ------------------------------------------------------


@dataclass
class CalendarEntry:
    row: int
    account: str
    run_at: datetime
    request: PublishRequest


class _CalendarRow(BaseModel):
    when: str
    files: list[str]
    type: str | None = None
    caption: str = ""
    account: str | None = None
    tz: str | None = None
    cover: str | None = None
    location: str | None = None


def _split_files(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(";") if p.strip()]


def read_calendar(path: Path) -> list[dict[str, Any]]:
    """Raw rows from a CSV (header row) or YAML (list, or ``{posts: [...]}``) calendar."""
    if not path.is_file():
        raise ScheduleError(f"Calendar file not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {
                (k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
                for k, v in r.items()
            }
            for r in reader
        ]
        return [r for r in rows if any(v for v in r.values())]
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text) or []
        if isinstance(data, dict):
            data = data.get("posts", [])
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ScheduleError("A YAML calendar must be a list of posts (or a 'posts:' list).")
        return [{str(k).lower(): v for k, v in r.items()} for r in data]
    raise ScheduleError(f"Unsupported calendar format '{suffix}' (use .csv, .yaml or .yml).")


def parse_calendar(
    rows: list[dict[str, Any]],
    *,
    default_account: str | None,
    base_dir: Path,
    tz: str | tzinfo | None = None,
    known_accounts: Iterable[str] | None = None,
    now: datetime | None = None,
) -> list[CalendarEntry]:
    """Validate every row; raise :class:`CalendarError` listing all problems, or return entries."""
    known = set(known_accounts) if known_accounts is not None else None
    entries: list[CalendarEntry] = []
    errors: list[str] = []
    for i, raw in enumerate(rows, start=1):
        label = f"row {i}"
        try:
            data = {k: v for k, v in raw.items() if v not in (None, "")}
            data["files"] = _split_files(data.get("files"))
            if "when" in data:
                data["when"] = str(data["when"])  # YAML may hand us a datetime
            if "caption" in data:
                data["caption"] = str(data["caption"])
            row = _CalendarRow.model_validate(data)
            account = row.account or default_account
            if not account:
                raise ScheduleError("no account (set an 'account' column or pass --account)")
            if known is not None and account not in known:
                raise ScheduleError(f"unknown account '{account}'")
            run_at = parse_when(row.when, row.tz or tz, now=now)
            req = build_request(
                row.files,
                row.type,
                row.caption,
                base_dir=base_dir,
                cover=row.cover,
                location=row.location,
            )
            entries.append(CalendarEntry(row=i, account=account, run_at=run_at, request=req))
        except ValidationError as e:
            missing = [".".join(map(str, err["loc"])) for err in e.errors()]
            errors.append(f"{label}: missing/invalid field(s): {', '.join(missing)}")
        except (ScheduleError, TimeParseError) as e:
            errors.append(f"{label}: {e}")
    if errors:
        raise CalendarError(errors)
    if not entries:
        raise ScheduleError("The calendar has no posts.")
    return entries


def enqueue_many(store: Store, entries: list[CalendarEntry]) -> list[int]:
    """Insert all entries in a single transaction (all or nothing)."""
    created = iso()
    ids: list[int] = []
    with store.tx() as c:
        for e in entries:
            cur = c.execute(
                "INSERT INTO scheduled_posts(account, request, run_at, created_at) "
                "VALUES (?,?,?,?)",
                (e.account, e.request.model_dump_json(), iso(e.run_at), created),
            )
            ids.append(int(cur.lastrowid))
    return ids


def import_calendar(
    store: Store,
    path: Path,
    *,
    default_account: str | None,
    tz: str | tzinfo | None = None,
    known_accounts: Iterable[str] | None = None,
    dry_run: bool = False,
) -> list[tuple[int | None, CalendarEntry]]:
    """Validate a calendar file and enqueue every row. Relative files resolve next to it."""
    rows = read_calendar(path)
    entries = parse_calendar(
        rows,
        default_account=default_account,
        base_dir=path.resolve().parent,
        tz=tz,
        known_accounts=known_accounts,
    )
    if dry_run:
        return [(None, e) for e in entries]
    return list(zip(enqueue_many(store, entries), entries, strict=True))
