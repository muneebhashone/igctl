from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from igctl.analytics import export as ex
from igctl.analytics import followers as fl
from igctl.analytics import metrics as mx
from igctl.analytics import report as rp
from igctl.backends.base import Backend, IgctlError, NotSupported
from igctl.cli import analyze
from igctl.core.config import Account
from igctl.core.context import Context
from igctl.core.models import (
    Insight,
    Media,
    MediaType,
    Profile,
    PublishRequest,
    PublishResult,
    UserSummary,
)
from igctl.core.storage import Store

MON = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)  # a Monday


def m(
    i: int,
    likes: int | None = 0,
    comments: int | None = 0,
    when: datetime | None = MON,
    mtype: MediaType = MediaType.IMAGE,
    caption: str | None = None,
    views: int | None = None,
    tags: list[str] | None = None,
) -> Media:
    return Media(
        id=str(i),
        code=f"c{i}",
        media_type=mtype,
        caption=caption,
        taken_at=when,
        like_count=likes,
        comment_count=comments,
        view_count=views,
        hashtags=tags or [],
    )


def u(i: int, name: str | None = None) -> UserSummary:
    return UserSummary(id=str(i), username=name or f"user{i}")


# --- metrics -------------------------------------------------------------------


def test_engagement_handles_missing_counts():
    assert mx.engagement(m(1, None, None)) == 0
    assert mx.engagement(m(1, 10, 2)) == 12


def test_engagement_rate_excludes_stories():
    media = [m(1, 90, 10), m(2, 190, 10), m(3, 10_000, 0, mtype=MediaType.STORY)]
    assert mx.engagement_rate(media, 1000) == pytest.approx(15.0)
    assert mx.engagement_rate(media, 0) is None
    assert mx.engagement_rate([], 1000) is None


def test_averages():
    a = mx.averages([m(1, 10, 2, views=100), m(2, 20, 4), m(3, 30, 0, views=300)])
    assert a["avg_likes"] == 20
    assert a["avg_comments"] == 2
    assert a["avg_engagement"] == pytest.approx(22)
    assert a["median_engagement"] == 24
    assert a["avg_views"] == 200
    assert mx.averages([])["avg_likes"] is None


def test_posts_per_week():
    media = [m(i, when=MON + timedelta(days=i)) for i in range(15)]  # 15 posts in 14 days
    assert mx.posts_per_week(media) == pytest.approx(7.0)
    assert mx.posts_per_week(media[:1]) is None
    assert mx.posts_per_week(media, now=MON + timedelta(days=28)) == pytest.approx(14 / 28 * 7)
    # Two posts a week apart are one post per week; same-day posts give no rate.
    assert mx.posts_per_week([m(1), m(2, when=MON + timedelta(days=7))]) == pytest.approx(1.0)
    assert mx.posts_per_week([m(1), m(2)]) is None


def test_best_weekday_and_hour_respect_timezone():
    media = [
        m(1, 100, when=datetime(2026, 1, 5, 23, 0, tzinfo=UTC)),  # Mon 23:00 UTC = Tue 04:00 +05
        m(2, 100, when=datetime(2026, 1, 12, 23, 30, tzinfo=UTC)),
        m(3, 10, when=datetime(2026, 1, 7, 9, 0, tzinfo=UTC)),
        m(4, 5, when=datetime(2026, 1, 8, 9, 0)),  # naive datetimes are treated as UTC
    ]
    assert mx.best_weekdays(media)[0]["weekday"] == "Mon"
    assert mx.best_hours(media)[0]["hour"] == "23:00"
    plus5 = mx.resolve_tz("+05:00")
    assert mx.best_weekdays(media, plus5)[0]["weekday"] == "Tue"
    assert mx.best_hours(media, plus5)[0]["hour"] == "04:00"
    assert mx.best_hours(media, plus5)[0]["posts"] == 2
    assert mx.best_weekdays(media)[0]["posts"] == 2


def test_resolve_tz():
    assert mx.resolve_tz(None) is UTC
    assert mx.resolve_tz("utc") is UTC
    assert mx.resolve_tz("UTC-3").utcoffset(None) == timedelta(hours=-3)
    assert mx.resolve_tz("+0530").utcoffset(None) == timedelta(hours=5, minutes=30)
    with pytest.raises(IgctlError):
        mx.resolve_tz("Not/AZone")


def test_by_media_type():
    media = [
        m(1, 10, mtype=MediaType.IMAGE),
        m(2, 30, mtype=MediaType.IMAGE),
        m(3, 100, mtype=MediaType.REEL, views=1000),
    ]
    rows = {r["media_type"]: r for r in mx.by_media_type(media, followers=100)}
    assert rows["image"]["posts"] == 2
    assert rows["image"]["avg_engagement"] == 20
    assert rows["image"]["share_pct"] == pytest.approx(200 / 3)
    assert rows["reel"]["avg_views"] == 1000
    assert rows["reel"]["avg_rate"] == 100
    assert rows["image"]["avg_views"] is None


def test_top_and_bottom_posts():
    media = [m(i, i * 10) for i in range(1, 6)]
    assert [r["id"] for r in mx.top_posts(media, 2)] == ["5", "4"]
    assert [r["id"] for r in mx.top_posts(media, 2, bottom=True)] == ["1", "2"]
    assert mx.top_posts(media, 1, followers=100)[0]["rate"] == 50


def test_hashtags_from_field_and_caption():
    assert mx.hashtags_of(m(1, tags=["#Food", "food", "travel"])) == ["food", "travel"]
    assert mx.hashtags_of(m(1, caption="Lunch #Food #café\n#food")) == ["food", "café"]


def test_hashtag_stats():
    media = [
        m(1, 100, caption="#a #b"),
        m(2, 10, caption="#a"),
        m(3, 50, caption="#b #c"),
        m(4, 1000, caption="#c #a", mtype=MediaType.STORY),  # ignored
    ]
    usage = mx.hashtag_stats(media)
    assert [(r["hashtag"], r["uses"]) for r in usage] == [
        ("#b", 2),
        ("#a", 2),
        ("#c", 1),
    ]  # ties by engagement
    by_eng = mx.top_hashtags_by_engagement(media, min_uses=2)
    assert [r["hashtag"] for r in by_eng] == ["#b", "#a"]
    assert by_eng[0]["avg_engagement"] == 75


def test_caption_length_stats():
    media = [
        m(1, 10, caption=None),
        m(2, 20, caption="x" * 40),
        m(3, 30, caption="x" * 200),
        m(4, 40, caption="x" * 400),
    ]
    out = mx.caption_length_stats(media)
    labels = [r["caption_length"] for r in out["buckets"]]
    assert labels == ["none", "1-50", "151-300", "301-1000"]
    assert out["correlation"] == pytest.approx(0.97, abs=0.03)
    assert mx.correlation([1, 1, 1], [1, 2, 3]) is None
    assert mx.correlation([1, 2], [1, 2]) is None


def test_co_occurring_hashtags():
    media = [m(1, 10, caption="#x #y"), m(2, 20, caption="#x #y #z"), m(3, 0, caption="#x")]
    rows = mx.co_occurring_hashtags(media, exclude="#X")
    assert rows[0] == {
        "hashtag": "#y",
        "posts": 2,
        "share_pct": pytest.approx(200 / 3),
        "avg_engagement": 15,
    }
    assert "#x" not in [r["hashtag"] for r in rows]


# --- reports -------------------------------------------------------------------


def sample_media() -> list[Media]:
    return [
        m(1, 100, 10, MON, MediaType.REEL, "Big day #launch #brand", views=5000),
        m(2, 50, 5, MON + timedelta(days=2), MediaType.IMAGE, "#brand"),
        m(3, 20, 1, MON + timedelta(days=4), MediaType.CAROUSEL, "short"),
        m(4, 80, 8, MON + timedelta(days=7), MediaType.REEL, "#launch #brand again", views=3000),
    ]


PROFILE = Profile(id="1", username="me", followers_count=1000, following_count=200, media_count=4)


def test_profile_report():
    report = rp.profile_report(PROFILE, sample_media())
    s = report["summary"]
    assert s["engagement_rate_pct"] == pytest.approx((110 + 55 + 21 + 88) / 4 / 1000 * 100)
    assert s["best_weekday"] == "Mon"
    assert s["top_media_type"] == "reel"
    assert s["analyzed_posts"] == 4
    assert s["followers_change"] is None
    assert report["top_posts"][0]["id"] == "1"
    assert report["hashtags_by_usage"][0]["hashtag"] == "#brand"
    assert report["hashtags_by_engagement"][0]["hashtag"] == "#launch"
    assert set(rp.SUMMARY_KEYS) == set(s)


def test_growth_vs_previous_snapshot():
    at = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    prev = {"at": at, "data": {"followers_count": 900, "following_count": 200, "media_count": 3}}
    g = rp.growth(PROFILE, prev)
    assert g["followers"] == 100
    assert g["following"] == 0
    assert g["media"] == 1
    assert g["followers_per_day"] == pytest.approx(10, rel=0.01)
    assert rp.growth(PROFILE, None) == {}


def test_compare_rows():
    other = PROFILE.model_copy(update={"username": "them", "followers_count": 10})
    reports = [rp.profile_report(PROFILE, sample_media()), rp.profile_report(other, [])]
    rows = {r["metric"]: r for r in rp.compare(reports)}
    assert rows["followers"] == {"metric": "followers", "me": 1000, "them": 10}
    assert rows["analyzed_posts"]["them"] == 0
    assert "username" not in rows


def test_hashtag_report():
    media = [
        m(1, 10, when=MON, caption="#tag #other"),
        m(2, 30, when=MON + timedelta(hours=12), caption="#TAG #other #more"),
    ]
    r = rp.hashtag_report(media, "#Tag")
    assert r["summary"]["hashtag"] == "#tag"
    assert r["summary"]["avg_engagement"] == 20
    assert r["summary"]["sample_posts_per_day"] == pytest.approx(2)
    assert r["related_hashtags"][0]["hashtag"] == "#other"


def test_insights_rows_and_totals():
    data = [
        Insight(name="reach", value=10, period="day", end_time=MON),
        Insight(name="reach", value=30, period="day", end_time=MON + timedelta(days=1)),
        Insight(name="follower_demographics", value={"country": {"US": 5, "PK": 9}}),
    ]
    rows = rp.insights_rows(data)
    assert len(rows) == 4
    assert rows[2]["key"] == "country.US"
    totals = rp.insights_totals(data)
    assert totals == [
        {
            "metric": "reach",
            "points": 2,
            "total": 40,
            "average": 20,
            "min": 10,
            "max": 30,
            "last": 30,
        }
    ]
    aud = rp.audience_rows(data[2:])
    assert [(r["dimension"], r["key"], r["value"]) for r in aud] == [
        ("country", "PK", 9),
        ("country", "US", 5),
    ]


# --- followers -----------------------------------------------------------------


def test_follower_diff():
    prev_fr = [u(1).model_dump(), u(2).model_dump(), u(3).model_dump()]
    cur_fr = fl.to_snapshot([u(2), u(3, "renamed"), u(4)])
    cur_fg = fl.to_snapshot([u(3), u(5)])
    d = fl.diff(cur_fr, cur_fg, prev_fr, fl.to_snapshot([u(3), u(6)]))
    names = {k: [x["username"] for x in v] for k, v in d.items()}
    assert names["new_followers"] == ["user4"]
    assert names["lost_followers"] == ["user1"]
    assert names["not_following_back"] == ["user5"]
    assert names["fans"] == ["user2", "user4"]
    assert names["new_following"] == ["user5"]
    assert names["unfollowed"] == ["user6"]
    assert fl.summary(d)["fans"] == 2
    assert set(fl.diff(cur_fr)) == set()  # nothing to compare


# --- export --------------------------------------------------------------------


def test_export_json_and_csv(tmp_path):
    report = rp.profile_report(PROFILE, sample_media())
    j = ex.export(report, tmp_path / "r.json")
    assert json.loads(j.read_text(encoding="utf-8"))["summary"]["username"] == "me"

    c = ex.export(report, tmp_path / "sub" / "r.csv")
    rows = list(csv.DictReader(c.open(encoding="utf-8")))
    assert {"section": "summary", "key": "username", "value": "me"} in rows
    assert any(r["section"] == "top_posts" and r["key"] == "0.id" for r in rows)

    t = ex.export([{"a": 1, "b": {"c": 2}}, {"a": 3, "d": 4}], tmp_path / "t.csv")
    rows = list(csv.DictReader(t.open(encoding="utf-8")))
    assert rows == [{"a": "1", "b.c": "2", "d": ""}, {"a": "3", "b.c": "", "d": "4"}]

    with pytest.raises(IgctlError):
        ex.export({}, tmp_path / "r.xml")


# --- CLI -----------------------------------------------------------------------


class FakeBackend(Backend):
    name = "graph"

    def __init__(self) -> None:
        self.fr = [u(1), u(2)]
        self.fg = [u(2), u(3)]

    def me(self) -> Profile:
        return PROFILE

    def publish(self, req: PublishRequest) -> PublishResult:  # pragma: no cover
        raise NotSupported(self.name, "publish")

    def get_profile(self, username: str) -> Profile:
        return PROFILE.model_copy(update={"username": username})

    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        return sample_media()[:limit]

    def get_media(self, media_id: str) -> Media:
        return sample_media()[0]

    def followers(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        yield from self.fr

    def following(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        yield from self.fg

    def hashtag_media(self, tag: str, limit: int = 50, recent: bool = False) -> list[Media]:
        return sample_media()

    def account_insights(self, metrics, period="day", days=30) -> list[Insight]:
        if metrics == ["follower_demographics"]:
            return [Insight(name="follower_demographics", value={"age": {"18-24": 3}})]
        return [Insight(name=n, value=5, period=period, end_time=MON) for n in metrics]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(Context, "backend", lambda self, prefer=None: backend)
    store = Store(tmp_path / "t.db")
    ic = Context(json_output=True)
    ic.__dict__["store"] = store
    ic.__dict__["account"] = Account(name="test")
    runner = CliRunner()

    def run(*args: str, ctx: Context = ic):
        result = runner.invoke(analyze.app, list(args), obj=ctx)
        return result

    run.backend = backend  # type: ignore[attr-defined]
    run.ctx = ic  # type: ignore[attr-defined]
    yield run
    store.close()


def _json(output: str):
    return json.loads(output)


def test_cli_profile_tracks_growth(cli, tmp_path):
    r = cli("profile", "--posts", "3", "--tz", "+02:00", "--out", str(tmp_path / "p.json"))
    assert r.exit_code == 0, r.output
    data = _json(r.stdout)
    assert data["summary"]["analyzed_posts"] == 3
    assert data["summary"]["followers_change"] is None
    assert (tmp_path / "p.json").exists()

    cli.backend.me = lambda: PROFILE.model_copy(update={"followers_count": 1100})
    r = cli("profile")
    assert _json(r.stdout)["summary"]["followers_change"] == 100


def test_cli_profile_table_mode(cli):
    cli.ctx.json_output = False
    r = cli("profile", "someone", "--no-snapshot")
    assert r.exit_code == 0, r.output
    assert "@someone" in r.output
    assert "Top posts" in r.output


def test_cli_compare(cli, tmp_path):
    r = cli("compare", "a", "b", "--out", str(tmp_path / "c.csv"))
    assert r.exit_code == 0, r.output
    rows = {row["metric"]: row for row in _json(r.stdout)}
    assert set(rows["followers"]) == {"metric", "a", "b"}
    assert cli("compare", "a").exit_code != 0


def test_cli_followers_diff(cli):
    r = cli("followers")
    assert r.exit_code == 0, r.output
    first = _json(r.stdout)
    assert [x["username"] for x in first["fans"]] == ["user1"]
    assert "new_followers" not in first

    cli.backend.fr = [u(2), u(4)]
    r = cli("followers", "--save", "--diff")
    d = _json(r.stdout)
    assert [x["username"] for x in d["new_followers"]] == ["user4"]
    assert [x["username"] for x in d["lost_followers"]] == ["user1"]
    assert [x["username"] for x in d["not_following_back"]] == ["user3"]

    r = cli("followers", "--diff", "nobody")
    assert r.exit_code == 1


def test_cli_post_falls_back_to_public_counts(cli):
    r = cli("post", "1")
    assert r.exit_code == 0, r.output
    data = _json(r.stdout)
    assert data["media"]["likes"] == 100
    assert data["insights"] == {}
    assert "media_insights" in data["insights_note"]


def test_cli_hashtag(cli):
    r = cli("hashtag", "#Brand", "--limit", "10")
    assert r.exit_code == 0, r.output
    data = _json(r.stdout)
    assert data["summary"]["hashtag"] == "#brand"
    assert data["related_hashtags"][0]["hashtag"] == "#launch"


def test_cli_insights_and_audience(cli, tmp_path):
    r = cli("insights", "--metrics", "reach,views", "--days", "7")
    assert r.exit_code == 0, r.output
    assert {t["metric"] for t in _json(r.stdout)["totals"]} == {"reach", "views"}

    r = cli("audience", "--out", str(tmp_path / "a.csv"))
    assert r.exit_code == 0, r.output
    assert _json(r.stdout)[0]["key"] == "18-24"
    assert (tmp_path / "a.csv").read_text(encoding="utf-8").startswith("metric,dimension")


def test_cli_not_supported_hint(cli, monkeypatch):
    def boom(*a, **k):
        raise NotSupported("graph", "hashtag_media")

    monkeypatch.setattr(cli.backend, "hashtag_media", boom)
    r = cli("hashtag", "x")
    assert r.exit_code == 1
    assert "--backend private" in " ".join(r.output.split())


# --- regressions -----------------------------------------------------------------


def test_follower_diff_skips_categories_of_truncated_snapshots():
    cur = fl.to_snapshot([u(1), u(2)])
    prev = fl.to_snapshot([u(2), u(3)])
    d = fl.diff(cur, cur, prev, prev, complete={"followers": False})
    assert "lost_followers" not in d and "new_followers" not in d
    assert "not_following_back" not in d and "fans" not in d
    assert set(d["skipped"]) == {"new_followers", "lost_followers", "not_following_back", "fans"}
    assert "new_following" in d  # the following lists are complete


def test_cli_followers_limit_is_flagged(cli):
    cli.backend.fr = [u(1), u(2), u(3)]
    assert cli("followers", "--limit", "2").exit_code == 0
    cli.backend.fr = [u(4), u(1), u(2)]  # u4 followed; the window pushes u2 out
    r = cli("followers", "--limit", "2")
    data = _json(r.stdout)
    assert "lost_followers" not in data and "lost_followers" in data["skipped"]
    assert "--limit" in r.stderr


def test_cli_dry_run_writes_no_snapshots(cli):
    ctx = Context(json_output=True, dry_run=True)
    ctx.__dict__["store"] = cli.ctx.store
    ctx.__dict__["account"] = cli.ctx.account
    assert cli("profile", ctx=ctx).exit_code == 0
    r = cli("followers", ctx=ctx)
    assert r.exit_code == 1  # nothing stored to diff yet
    assert cli.ctx.store._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


def test_cli_post_accepts_url_and_shortcode(cli):
    seen = []
    cli.backend.get_media = lambda media_id: seen.append(media_id) or sample_media()[0]
    code = sample_media()[0].code
    r = cli("post", f"https://www.instagram.com/p/{code}/")
    assert r.exit_code == 0, r.output
    assert seen == [sample_media()[0].id]  # graph: shortcode looked up among own posts


def test_cli_insights_rejects_unsupported_period(cli):
    r = cli("insights", "--period", "week")
    assert r.exit_code != 0 and "day" in r.output


def test_resolve_tz_rejects_out_of_range_offsets():
    from igctl.scheduler.timeparse import TimeParseError
    from igctl.scheduler.timeparse import resolve_tz as sched_tz

    with pytest.raises(IgctlError, match="offset"):
        mx.resolve_tz("+25")
    with pytest.raises(TimeParseError, match="offset"):
        sched_tz("+24:00")
    assert sched_tz("-23:59") is not None
