"""`igctl analyze ...` - profile, post, hashtag, follower and account analytics."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer

from igctl.analytics import followers as fl
from igctl.analytics import report as rp
from igctl.analytics.export import export
from igctl.analytics.metrics import resolve_tz
from igctl.backends.base import NotSupported
from igctl.cli._common import emit, get_ctx, ok, warn
from igctl.cli.download import resolve_media_ref
from igctl.core.context import Context

app = typer.Typer(no_args_is_help=True, help="Profile, post, hashtag and follower analytics.")

OutOpt = typer.Option(None, "--out", "-o", help="Also write the report to a .json or .csv file.")
POST_COLUMNS = ["code", "media_type", "taken_at", "likes", "comments", "views", "rate", "caption"]
USER_COLUMNS = ["username", "id"]
DEFAULT_INSIGHTS = "reach,views,accounts_engaged,total_interactions,follower_count"


@contextmanager
def _supported() -> Iterator[None]:
    """Turn NotSupported into a friendly hint to switch backends."""
    try:
        yield
    except NotSupported as e:
        other = "private" if e.backend == "graph" else "graph"
        warn(f"{e} Try `igctl --backend {other} analyze ...`.")
        raise typer.Exit(1) from e


def _save(data: Any, out: Path | None) -> None:
    if out:
        ok(f"Report written to {export(data, out)}")


def _sections(ic: Context, report: dict, sections: list[tuple[str, str, list[str] | None]]) -> None:
    for key, title, cols in sections:
        if report.get(key):
            emit(ic, report[key], cols, title=title)


@app.command()
def profile(
    ctx: typer.Context,
    username: str | None = typer.Argument(None, help="Account to analyze (default: you)."),
    posts: int = typer.Option(30, "--posts", "-n", min=1, help="Recent posts to analyze."),
    tz: str = typer.Option("UTC", "--tz", help="Time zone for best-time analysis."),
    top: int = typer.Option(5, "--top", min=1, help="Top/bottom posts to list."),
    snapshot: bool = typer.Option(
        True, "--snapshot/--no-snapshot", help="Store counts to track growth over time."
    ),
    out: Path | None = OutOpt,
) -> None:
    """Engagement, best posting times, content mix, hashtags and growth for an account."""
    ic = get_ctx(ctx)
    zone = resolve_tz(tz)
    # --dry-run changes nothing, including the stored growth baseline.
    save = snapshot and not ic.dry_run
    with _supported():
        report = rp.build_profile_report(
            ic.backend(), username, posts, zone, ic.store, ic.account.name, save, top
        )
    _save(report, out)
    if ic.json_output:
        emit(ic, report)
        return
    emit(ic, report["summary"], title=f"@{report['summary']['username']}")
    _sections(
        ic,
        report,
        [
            ("media_types", "Media types", None),
            ("weekdays", f"Best weekdays ({report['timezone']})", None),
            ("hours", f"Best hours ({report['timezone']})", None),
            ("top_posts", "Top posts", POST_COLUMNS),
            ("bottom_posts", "Bottom posts", POST_COLUMNS),
            ("hashtags_by_usage", "Hashtags by usage", None),
            ("hashtags_by_engagement", "Hashtags by avg engagement (2+ uses)", None),
            ("caption_length", "Caption length vs engagement", None),
        ],
    )


@app.command()
def compare(
    ctx: typer.Context,
    usernames: Annotated[list[str], typer.Argument(help="Two or more usernames.")],
    posts: int = typer.Option(30, "--posts", "-n", min=1),
    tz: str = typer.Option("UTC", "--tz"),
    out: Path | None = OutOpt,
) -> None:
    """Side-by-side profile reports."""
    if len(usernames) < 2:
        raise typer.BadParameter("give at least two usernames", param_hint="USERNAMES")
    ic = get_ctx(ctx)
    zone = resolve_tz(tz)
    backend = ic.backend()
    with _supported():
        reports = [rp.build_profile_report(backend, u, posts, zone) for u in usernames]
    rows = rp.compare(reports)
    _save(rows, out)
    emit(ic, rows, title="Comparison")


@app.command()
def followers(
    ctx: typer.Context,
    username: str | None = typer.Argument(None, help="Account (default: you)."),
    save: bool = typer.Option(False, "--save", help="Take a new follower/following snapshot."),
    diff: bool = typer.Option(False, "--diff", help="Diff the two latest snapshots."),
    limit: int = typer.Option(0, "--limit", min=0, help="Max users to fetch (0 = all)."),
    show: int = typer.Option(25, "--show", min=0, help="Max users listed per category."),
    out: Path | None = OutOpt,
) -> None:
    """Snapshot followers and show new/lost followers, not-following-back and fans.

    Without --save or --diff, does both: snapshot now, then diff against the previous one.
    """
    ic = get_ctx(ctx)
    if not save and not diff:
        save = diff = True
    account = ic.account.name
    subject = username
    if save and ic.dry_run:
        warn("[dry-run] not saving a follower snapshot; diffing the stored ones.")
        save = False
    if save:
        with _supported():
            counts = fl.take_snapshot(ic.backend(), ic.store, account, username, limit)
        subject = counts["username"]
        ok(
            f"Saved snapshot of @{subject}: "
            + ", ".join(f"{v} {k}" for k, v in counts.items() if k != "username")
        )
    if not diff:
        return
    if subject is None:
        with _supported():
            subject = ic.backend().me().username
    result = fl.diff_snapshots(ic.store, account, subject)
    if not result:
        warn(f"No follower snapshots for @{subject} yet. Run with --save first.")
        raise typer.Exit(1)
    if result.get("previous_at") is None:
        warn("Only one snapshot so far; new/lost followers appear after the next --save.")
    if skipped := result.get("skipped"):
        warn(
            "Snapshots were cut by --limit, so these can't be computed reliably: "
            + ", ".join(skipped)
            + ". Snapshot without --limit for a full diff."
        )
    _save(result, out)
    if ic.json_output:
        emit(ic, result)
        return
    emit(ic, fl.summary(result), title=f"@{subject} follower diff")
    for key, title in (
        ("new_followers", "New followers"),
        ("lost_followers", "Lost followers (unfollowed you)"),
        ("not_following_back", "Not following you back"),
        ("fans", "Fans (you don't follow back)"),
        ("new_following", "You started following"),
        ("unfollowed", "You unfollowed"),
    ):
        users = result.get(key) or []
        if users and show:
            more = f" (showing {show} of {len(users)})" if len(users) > show else ""
            emit(ic, users[:show], USER_COLUMNS, title=title + more)


@app.command()
def post(
    ctx: typer.Context,
    media_id: str = typer.Argument(..., help="Media id, shortcode or URL."),
    metrics: str | None = typer.Option(None, "--metrics", help="Comma-separated insight metrics."),
    out: Path | None = OutOpt,
) -> None:
    """Public counts and private insights (when available) for one post."""
    ic = get_ctx(ctx)
    names = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    with _supported():
        backend = ic.backend()
        media_id = resolve_media_ref(backend, media_id)
        report = rp.post_report(backend, media_id, names)
    _save(report, out)
    if ic.json_output:
        emit(ic, report)
        return
    emit(ic, report["media"], title=f"Post {media_id}")
    if report["insights"]:
        emit(ic, report["insights"], title="Insights")
    if note := report.get("insights_note"):
        warn(f"{note} Showing public counts only.")


@app.command()
def hashtag(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="Hashtag, with or without #."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="Posts to sample."),
    recent: bool = typer.Option(False, "--recent", help="Sample recent instead of top posts."),
    out: Path | None = OutOpt,
) -> None:
    """Engagement, volume and related hashtags for a hashtag."""
    ic = get_ctx(ctx)
    clean = tag.lstrip("#")
    with _supported():
        media = ic.backend().hashtag_media(clean, limit=limit, recent=recent)
    report = rp.hashtag_report(media, clean)
    _save(report, out)
    if ic.json_output:
        emit(ic, report)
        return
    emit(ic, report["summary"], title=f"#{clean.lower()}")
    _sections(
        ic,
        report,
        [
            ("media_types", "Media types", None),
            ("related_hashtags", "Related hashtags", None),
            ("top_posts", "Top posts", POST_COLUMNS),
        ],
    )


@app.command()
def insights(
    ctx: typer.Context,
    days: int = typer.Option(30, "--days", "-d", min=1, help="Look-back window."),
    metrics: str = typer.Option(DEFAULT_INSIGHTS, "--metrics", help="Comma-separated metrics."),
    period: str = typer.Option(
        "day",
        "--period",
        help="Only 'day' (the Graph API's sole period for these metrics); --days sets the range.",
    ),
    out: Path | None = OutOpt,
) -> None:
    """Account insights over a period (Graph API)."""
    if period != "day":
        raise typer.BadParameter(
            "the Graph API only supports period 'day' for account metrics; use --days for "
            "the range",
            param_hint="--period",
        )
    ic = get_ctx(ctx)
    names = [m.strip() for m in metrics.split(",") if m.strip()]
    with _supported():
        data = ic.backend().account_insights(names, period=period, days=days)
    rows = rp.insights_rows(data)
    totals = rp.insights_totals(data)
    _save({"totals": totals, "rows": rows}, out)
    if ic.json_output:
        emit(ic, {"totals": totals, "rows": rows})
        return
    if not rows:
        warn("No insight data returned.")
        return
    if totals:
        emit(ic, totals, title=f"Totals, last {days} days")
    emit(ic, rows, ["metric", "end_time", "key", "value"], title="Insights")


@app.command()
def audience(
    ctx: typer.Context,
    metric: str = typer.Option(
        "follower_demographics",
        "--metric",
        help="follower_demographics | engaged_audience_demographics",
    ),
    out: Path | None = OutOpt,
) -> None:
    """Follower demographics (age, gender, city, country) where the backend supports it."""
    ic = get_ctx(ctx)
    with _supported():
        data = ic.backend().account_insights([metric], period="lifetime")
    rows = rp.audience_rows(data)
    _save(rows, out)
    if not rows:
        warn("No demographic data (Instagram requires 100+ followers).")
        if ic.json_output:
            emit(ic, [])
        return
    emit(ic, rows, ["dimension", "key", "value"], title="Audience")
