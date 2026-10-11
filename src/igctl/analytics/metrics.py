"""Pure, backend-agnostic metrics over lists of :class:`~igctl.core.models.Media`.

Every function here is deterministic and side-effect free so it can be unit-tested with
synthetic media. Missing counts (``None``, e.g. hidden like counts) are treated as 0.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from igctl.backends.base import IgctlError
from igctl.core.models import Media, MediaType

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Caption length buckets (inclusive upper bounds, in characters).
CAPTION_BUCKETS: tuple[tuple[str, int], ...] = (
    ("none", 0),
    ("1-50", 50),
    ("51-150", 150),
    ("151-300", 300),
    ("301-1000", 1000),
    ("1000+", 10**9),
)

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


# --- primitives ----------------------------------------------------------------


def likes(m: Media) -> int:
    return m.like_count or 0


def comments(m: Media) -> int:
    return m.comment_count or 0


def engagement(m: Media) -> int:
    """Interactions on a post: likes + comments."""
    return likes(m) + comments(m)


def mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None


def rate(m: Media, followers: int | None) -> float | None:
    """Engagement rate of one post, as a percentage of ``followers``."""
    if not followers:
        return None
    return engagement(m) / followers * 100


def feed_posts(media: Iterable[Media]) -> list[Media]:
    """Media excluding stories (stories have no public engagement counts)."""
    return [m for m in media if m.media_type != MediaType.STORY]


def hashtags_of(m: Media) -> list[str]:
    """Lower-cased, de-duplicated hashtags (falls back to parsing the caption)."""
    tags = m.hashtags or _HASHTAG_RE.findall(m.caption or "")
    return list(dict.fromkeys(t.lstrip("#").lower() for t in tags if t))


def resolve_tz(name: str | None) -> tzinfo:
    """``UTC``, a fixed offset like ``+05:00`` / ``UTC-3``, or an IANA zone name."""
    if not name or name.upper() in ("UTC", "Z", "GMT"):
        return UTC
    if match := _OFFSET_RE.match(name.strip()):
        sign, hours, minutes = match.groups()
        if int(hours) > 23 or int(minutes or 0) > 59:
            raise IgctlError(f"Invalid UTC offset '{name}' (hours 0-23, minutes 0-59).")
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
        return timezone(-delta if sign == "-" else delta, name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise IgctlError(
            f"Unknown time zone '{name}'. Use an IANA name (requires the 'tzdata' package "
            "on Windows) or a fixed offset such as +05:00."
        ) from e


def as_tz(dt: datetime, tz: tzinfo) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz)


# --- aggregate metrics ---------------------------------------------------------


def engagement_rate(media: Sequence[Media], followers: int | None) -> float | None:
    """Average (likes + comments) / followers over ``media``, as a percentage."""
    posts = feed_posts(media)
    if not posts or not followers:
        return None
    return mean(engagement(m) for m in posts) / followers * 100  # type: ignore[operator]


def averages(media: Sequence[Media]) -> dict[str, float | None]:
    posts = feed_posts(media)
    viewed = [m.view_count for m in posts if m.view_count is not None]
    return {
        "avg_likes": mean(likes(m) for m in posts),
        "avg_comments": mean(comments(m) for m in posts),
        "avg_engagement": mean(engagement(m) for m in posts),
        "median_engagement": statistics.median(engagement(m) for m in posts) if posts else None,
        "avg_views": mean(viewed),
    }


def posts_per_week(media: Sequence[Media], now: datetime | None = None) -> float | None:
    """Posting frequency over the span covered by ``media``.

    N posts span N-1 intervals, so the rate is (N-1) / span: two posts a week apart are one
    post per week. The span runs from the oldest post to ``now`` (default: the newest post),
    so a long silence since the last post lowers the rate. Needs at least two dated posts
    and a non-zero span.
    """
    dates = sorted(as_tz(m.taken_at, UTC) for m in media if m.taken_at)
    if len(dates) < 2:
        return None
    end = max(as_tz(now, UTC), dates[-1]) if now else dates[-1]
    days = (end - dates[0]).total_seconds() / 86400
    if days <= 0:
        return None
    return (len(dates) - 1) / days * 7


def _group_stats(
    media: Iterable[Media], key: Callable[[Media], object], followers: int | None = None
) -> dict[object, dict[str, float | int | None]]:
    groups: dict[object, list[Media]] = defaultdict(list)
    for m in media:
        groups[key(m)].append(m)
    out: dict[object, dict[str, float | int | None]] = {}
    for k, items in groups.items():
        avg = mean(engagement(m) for m in items)
        out[k] = {
            "posts": len(items),
            "avg_engagement": avg,
            "avg_rate": avg / followers * 100 if followers and avg is not None else None,
        }
    return out


def best_weekdays(media: Sequence[Media], tz: tzinfo = UTC) -> list[dict]:
    """Average engagement per weekday (in ``tz``), best first."""
    dated = [m for m in feed_posts(media) if m.taken_at]
    stats = _group_stats(dated, lambda m: as_tz(m.taken_at, tz).weekday())  # type: ignore[arg-type]
    rows = [{"weekday": WEEKDAYS[k], **v} for k, v in stats.items()]  # type: ignore[index]
    return sorted(rows, key=lambda r: (-(r["avg_engagement"] or 0), -r["posts"]))


def best_hours(media: Sequence[Media], tz: tzinfo = UTC) -> list[dict]:
    """Average engagement per hour of day (in ``tz``), best first."""
    dated = [m for m in feed_posts(media) if m.taken_at]
    stats = _group_stats(dated, lambda m: as_tz(m.taken_at, tz).hour)  # type: ignore[arg-type]
    rows = [{"hour": f"{k:02d}:00", **v} for k, v in stats.items()]
    return sorted(rows, key=lambda r: (-(r["avg_engagement"] or 0), -r["posts"]))


def by_media_type(media: Sequence[Media], followers: int | None = None) -> list[dict]:
    """Mix and performance per media type, most-used first."""
    posts = list(media)
    total = len(posts) or 1
    rows = []
    for mtype, items in _bucket(posts, lambda m: m.media_type.value).items():
        views = [m.view_count for m in items if m.view_count is not None]
        avg = mean(engagement(m) for m in items)
        rows.append(
            {
                "media_type": mtype,
                "posts": len(items),
                "share_pct": len(items) / total * 100,
                "avg_likes": mean(likes(m) for m in items),
                "avg_comments": mean(comments(m) for m in items),
                "avg_engagement": avg,
                "avg_views": mean(views),
                "avg_rate": avg / followers * 100 if followers and avg is not None else None,
            }
        )
    return sorted(rows, key=lambda r: -r["posts"])


def _bucket(media: Iterable[Media], key: Callable[[Media], str]) -> dict[str, list[Media]]:
    out: dict[str, list[Media]] = defaultdict(list)
    for m in media:
        out[key(m)].append(m)
    return out


def post_row(m: Media, followers: int | None = None) -> dict:
    caption = (m.caption or "").replace("\n", " ")
    return {
        "id": m.id,
        "code": m.code,
        "media_type": m.media_type.value,
        "taken_at": m.taken_at.isoformat() if m.taken_at else None,
        "likes": m.like_count,
        "comments": m.comment_count,
        "views": m.view_count,
        "engagement": engagement(m),
        "rate": rate(m, followers),
        "permalink": m.permalink,
        "caption": caption[:60] + ("..." if len(caption) > 60 else ""),
    }


def top_posts(
    media: Sequence[Media], n: int = 5, followers: int | None = None, bottom: bool = False
) -> list[dict]:
    """The ``n`` best (or worst, with ``bottom``) feed posts by engagement."""
    ranked = sorted(feed_posts(media), key=engagement, reverse=not bottom)
    return [post_row(m, followers) for m in ranked[:n]]


def hashtag_stats(media: Sequence[Media], min_uses: int = 1) -> list[dict]:
    """Usage count and average engagement of posts carrying each hashtag, most-used first."""
    uses: Counter[str] = Counter()
    eng: dict[str, list[int]] = defaultdict(list)
    for m in feed_posts(media):
        for tag in hashtags_of(m):
            uses[tag] += 1
            eng[tag].append(engagement(m))
    rows = [
        {"hashtag": f"#{t}", "uses": c, "avg_engagement": mean(eng[t])}
        for t, c in uses.items()
        if c >= min_uses
    ]
    return sorted(rows, key=lambda r: (-r["uses"], -(r["avg_engagement"] or 0), r["hashtag"]))


def top_hashtags_by_engagement(
    media: Sequence[Media], n: int = 10, min_uses: int = 2
) -> list[dict]:
    """Hashtags used at least ``min_uses`` times, ranked by average engagement."""
    rows = hashtag_stats(media, min_uses=min_uses)
    return sorted(rows, key=lambda r: (-(r["avg_engagement"] or 0), -r["uses"]))[:n]


def caption_length_stats(media: Sequence[Media]) -> dict:
    """Engagement per caption-length bucket plus the length/engagement correlation."""
    posts = feed_posts(media)
    buckets: dict[str, list[Media]] = {label: [] for label, _ in CAPTION_BUCKETS}
    for m in posts:
        length = len(m.caption or "")
        label = next(lbl for lbl, upper in CAPTION_BUCKETS if length <= upper)
        buckets[label].append(m)
    rows = [
        {
            "caption_length": label,
            "posts": len(items),
            "avg_engagement": mean(engagement(m) for m in items),
        }
        for label, items in buckets.items()
        if items
    ]
    return {
        "buckets": rows,
        "correlation": correlation(
            [len(m.caption or "") for m in posts], [engagement(m) for m in posts]
        ),
    }


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or None when undefined (too few points / zero variance)."""
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def co_occurring_hashtags(media: Sequence[Media], exclude: str = "", n: int = 20) -> list[dict]:
    """Hashtags appearing alongside each other in ``media`` (minus ``exclude``)."""
    exclude = exclude.lstrip("#").lower()
    counts: Counter[str] = Counter()
    eng: dict[str, list[int]] = defaultdict(list)
    for m in media:
        for tag in hashtags_of(m):
            if tag != exclude:
                counts[tag] += 1
                eng[tag].append(engagement(m))
    total = len(media) or 1
    return [
        {
            "hashtag": f"#{t}",
            "posts": c,
            "share_pct": c / total * 100,
            "avg_engagement": mean(eng[t]),
        }
        for t, c in counts.most_common(n)
    ]
