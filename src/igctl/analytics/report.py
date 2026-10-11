"""Report builders that combine backend data with the pure metrics in :mod:`.metrics`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, tzinfo
from typing import Any

from igctl.analytics import metrics as mx
from igctl.backends.base import Backend, NotSupported
from igctl.core.models import Insight, Media, Profile
from igctl.core.storage import Store

# Headline numbers shown first by `analyze profile` and used as rows by `analyze compare`.
SUMMARY_KEYS = (
    "username",
    "followers",
    "following",
    "posts",
    "analyzed_posts",
    "engagement_rate_pct",
    "avg_likes",
    "avg_comments",
    "avg_engagement",
    "median_engagement",
    "avg_views",
    "posts_per_week",
    "best_weekday",
    "best_hour",
    "top_media_type",
    "caption_len_correlation",
    "followers_change",
    "followers_change_since",
)


# --- profile -------------------------------------------------------------------


def profile_snapshot(profile: Profile) -> dict[str, Any]:
    return {
        "followers_count": profile.followers_count,
        "following_count": profile.following_count,
        "media_count": profile.media_count,
    }


def growth(profile: Profile, previous: dict | None) -> dict[str, Any]:
    """Change in counts vs a stored ``{"at", "data"}`` profile snapshot."""
    if not previous:
        return {}
    prev = previous["data"]
    out: dict[str, Any] = {"since": previous["at"]}
    for key in ("followers_count", "following_count", "media_count"):
        now, before = getattr(profile, key), prev.get(key)
        out[key.removesuffix("_count")] = (
            now - before if now is not None and before is not None else None
        )
    try:
        days = (datetime.now(UTC) - datetime.fromisoformat(previous["at"])).total_seconds() / 86400
    except (TypeError, ValueError):
        days = 0
    if out.get("followers") is not None and days >= 1:
        out["followers_per_day"] = out["followers"] / days
    return out


def profile_report(
    profile: Profile,
    media: Sequence[Media],
    tz: tzinfo = UTC,
    previous: dict | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """Full analytics for one account. ``previous`` is an earlier profile snapshot."""
    followers = profile.followers_count
    posts = mx.feed_posts(media)
    weekdays = mx.best_weekdays(posts, tz)
    hours = mx.best_hours(posts, tz)
    types = mx.by_media_type(posts, followers)
    captions = mx.caption_length_stats(posts)
    grow = growth(profile, previous)
    summary: dict[str, Any] = {
        "username": profile.username,
        "followers": followers,
        "following": profile.following_count,
        "posts": profile.media_count,
        "analyzed_posts": len(posts),
        "engagement_rate_pct": mx.engagement_rate(posts, followers),
        **mx.averages(posts),
        "posts_per_week": mx.posts_per_week(posts),
        "best_weekday": weekdays[0]["weekday"] if weekdays else None,
        "best_hour": hours[0]["hour"] if hours else None,
        "top_media_type": max(types, key=lambda r: r["avg_engagement"] or 0)["media_type"]
        if types
        else None,
        "caption_len_correlation": captions["correlation"],
        "followers_change": grow.get("followers"),
        "followers_change_since": grow.get("since"),
    }
    return {
        "summary": summary,
        "timezone": str(tz),
        "growth": grow,
        "media_types": types,
        "weekdays": weekdays,
        "hours": hours,
        "top_posts": mx.top_posts(posts, top_n, followers),
        "bottom_posts": mx.top_posts(posts, top_n, followers, bottom=True),
        "hashtags_by_usage": mx.hashtag_stats(posts)[:15],
        "hashtags_by_engagement": mx.top_hashtags_by_engagement(posts, 15),
        "caption_length": captions["buckets"],
    }


def build_profile_report(
    backend: Backend,
    username: str | None,
    posts: int = 30,
    tz: tzinfo = UTC,
    store: Store | None = None,
    account: str | None = None,
    snapshot: bool = True,
    top_n: int = 5,
) -> dict[str, Any]:
    """Fetch ``username`` (None = me) and build its report.

    With ``store`` and ``account``, growth is computed against the previous profile
    snapshot and (if ``snapshot``) the current counts are saved for next time.
    """
    profile = backend.get_profile(username) if username else backend.me()
    media = backend.list_media(username, limit=posts)
    previous = None
    if store and account:
        snaps = store.snapshots(account, "profile", profile.username, limit=1)
        previous = snaps[0] if snaps else None
        if snapshot:
            store.save_snapshot(account, "profile", profile.username, profile_snapshot(profile))
    return profile_report(profile, media, tz, previous, top_n)


def compare(reports: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Side-by-side rows: one per summary metric, one column per username."""
    names = [r["summary"]["username"] for r in reports]
    rows = []
    for key in SUMMARY_KEYS[1:]:
        if key.startswith("followers_change"):
            continue
        rows.append(
            {
                "metric": key,
                **{n: r["summary"].get(key) for n, r in zip(names, reports, strict=True)},
            }
        )
    return rows


# --- single post ---------------------------------------------------------------


def post_report(
    backend: Backend, media_id: str, metrics: list[str] | None = None
) -> dict[str, Any]:
    """Public counts plus private insights when the backend supports them."""
    media: Media | None = None
    try:
        media = backend.get_media(media_id)
    except NotSupported:
        pass
    insights: list[Insight] = []
    insights_note = None
    try:
        insights = backend.media_insights(media_id, metrics)
    except NotSupported as e:
        insights_note = str(e)
    if media is None and not insights:
        raise NotSupported(backend.name, "get_media/media_insights")
    report: dict[str, Any] = {"media": mx.post_row(media) if media else {"id": media_id}}
    if media:
        report["media"]["hashtags"] = " ".join(f"#{t}" for t in mx.hashtags_of(media))
    report["insights"] = {i.name: i.value for i in insights}
    if insights_note:
        report["insights_note"] = insights_note
    return report


# --- hashtag research ----------------------------------------------------------


def hashtag_report(media: Sequence[Media], tag: str, top_n: int = 5) -> dict[str, Any]:
    """Engagement, volume and related tags from a sample of ``tag``'s media."""
    tag = tag.lstrip("#").lower()
    posts = mx.feed_posts(media)
    avgs = mx.averages(posts)
    dates = sorted(mx.as_tz(m.taken_at, UTC) for m in posts if m.taken_at)
    per_day = None
    if len(dates) >= 2:
        span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1 / 24)
        per_day = len(dates) / span_days
    summary = {
        "hashtag": f"#{tag}",
        "sampled_posts": len(posts),
        "avg_likes": avgs["avg_likes"],
        "avg_comments": avgs["avg_comments"],
        "avg_engagement": avgs["avg_engagement"],
        "median_engagement": avgs["median_engagement"],
        "avg_views": avgs["avg_views"],
        "sample_posts_per_day": per_day,
        "sample_from": dates[0].isoformat() if dates else None,
        "sample_to": dates[-1].isoformat() if dates else None,
        "avg_hashtags_per_post": mx.mean(len(mx.hashtags_of(m)) for m in posts),
    }
    return {
        "summary": summary,
        "media_types": mx.by_media_type(posts),
        "related_hashtags": mx.co_occurring_hashtags(posts, exclude=tag, n=25),
        "top_posts": mx.top_posts(posts, top_n),
    }


# --- account insights ----------------------------------------------------------


def insights_rows(insights: Sequence[Insight]) -> list[dict[str, Any]]:
    """Flatten insights to table rows; dict values become one row per key."""
    rows: list[dict[str, Any]] = []
    for i in insights:
        base = {
            "metric": i.name,
            "period": i.period,
            "end_time": i.end_time.isoformat() if i.end_time else None,
        }
        if isinstance(i.value, dict):
            for k, v in _flatten(i.value).items():
                rows.append({**base, "key": k, "value": v})
        else:
            rows.append({**base, "key": None, "value": i.value})
    return rows


def insights_totals(insights: Sequence[Insight]) -> list[dict[str, Any]]:
    """Per-metric total / average / last value over numeric insights."""
    by_metric: dict[str, list[float]] = {}
    for i in insights:
        if isinstance(i.value, int | float) and not isinstance(i.value, bool):
            by_metric.setdefault(i.name, []).append(i.value)
    return [
        {
            "metric": name,
            "points": len(vals),
            "total": sum(vals),
            "average": sum(vals) / len(vals),
            "min": min(vals),
            "max": max(vals),
            "last": vals[-1],
        }
        for name, vals in by_metric.items()
    ]


def audience_rows(insights: Sequence[Insight]) -> list[dict[str, Any]]:
    """Demographic breakdown rows (``dimension``, ``key``, ``value``), largest first."""
    rows = []
    for i in insights:
        if isinstance(i.value, dict):
            for path, v in _flatten(i.value).items():
                dim, _, key = path.rpartition(".")
                rows.append({"metric": i.name, "dimension": dim or i.name, "key": key, "value": v})
        elif i.value is not None:
            rows.append({"metric": i.name, "dimension": i.name, "key": "", "value": i.value})
    return sorted(rows, key=lambda r: (r["dimension"], -_num(r["value"])))


def _num(v: Any) -> float:
    return float(v) if isinstance(v, int | float) else 0.0


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out
