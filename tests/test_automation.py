from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from igctl.automation import Engine, RulesError, TextFilter, parse_rules
from igctl.automation.rules import load_rules, next_run, parse_interval
from igctl.backends.base import Backend
from igctl.cli.automate import app as automate_app
from igctl.core.config import Account, Config, Limits
from igctl.core.context import Context
from igctl.core.models import (
    Comment,
    DirectMessage,
    DirectThread,
    Media,
    MediaType,
    Profile,
    PublishRequest,
    PublishResult,
    UserSummary,
)
from igctl.core.storage import iso

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "rules.yaml"


def _user(name: str, **kw) -> UserSummary:
    return UserSummary(id=f"id-{name}", username=name, **kw)


class FakeBackend(Backend):
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.media: list[Media] = [Media(id="m1", media_type=MediaType.IMAGE, permalink="p1")]
        self.comments: dict[str, list[Comment]] = {}
        self.threads: list[DirectThread] = []
        self.follower_list: list[UserSummary] = []
        self.following_list: list[UserSummary] = []
        self.tag_media: dict[str, list[Media]] = {}

    def me(self) -> Profile:
        return Profile(id="me-id", username="me")

    def publish(self, req: PublishRequest) -> PublishResult:
        raise NotImplementedError

    def get_profile(self, username: str) -> Profile:
        return Profile(id=f"id-{username}", username=username)

    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        return self.media[:limit]

    def list_comments(self, media_id: str, limit: int = 100) -> list[Comment]:
        return self.comments.get(media_id, [])[:limit]

    def followers(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        yield from self.follower_list[: limit or None]

    def following(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        yield from self.following_list[: limit or None]

    def hashtag_media(self, tag: str, limit: int = 50, recent: bool = False) -> list[Media]:
        return self.tag_media.get(tag, [])[:limit]

    def list_threads(self, limit: int = 20, unread_only: bool = False) -> list[DirectThread]:
        return self.threads[:limit]

    # writes
    def comment(self, media_id: str, text: str, reply_to: str | None = None) -> Comment:
        self.calls.append(("comment", media_id, text, reply_to))
        return Comment(id="new", media_id=media_id, text=text, username="me")

    def hide_comment(self, comment_id: str, hide: bool = True) -> None:
        self.calls.append(("hide", comment_id))

    def like(self, media_id: str) -> None:
        self.calls.append(("like", media_id))

    def follow(self, username: str) -> None:
        self.calls.append(("follow", username))

    def unfollow(self, username: str) -> None:
        self.calls.append(("unfollow", username))

    def send_dm(self, usernames: list[str], text: str) -> DirectMessage:
        self.calls.append(("send_dm", tuple(usernames), text))
        return DirectMessage(id="dm", thread_id="t")

    def reply_dm(self, thread_id: str, text: str) -> DirectMessage:
        self.calls.append(("reply_dm", thread_id, text))
        return DirectMessage(id="dm", thread_id=thread_id)

    def mark_thread_seen(self, thread_id: str) -> None:
        self.calls.append(("seen", thread_id))


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))
    monkeypatch.delenv("IGCTL_ACCOUNT", raising=False)
    limits = Limits(min_delay=0, max_delay=0)
    Config(
        default_account="test",
        accounts={"test": Account(name="test", backend="private", limits=limits)},
    ).save()
    return tmp_path


@pytest.fixture
def fake() -> FakeBackend:
    return FakeBackend()


def make_ctx(fake: FakeBackend, dry_run: bool = False) -> Context:
    ctx = Context(account_name="test", dry_run=dry_run)
    ctx._backends.update(graph=fake, private=fake)
    return ctx


def make_engine(rules: list[dict], ctx: Context) -> Engine:
    return Engine(parse_rules(rules), lambda _acct: ctx, sleep=lambda _s: None)


def run(rules: list[dict], fake: FakeBackend, dry_run: bool = False, force: bool = True):
    return make_engine(rules, make_ctx(fake, dry_run)).run_once(force=force)


# --- schema ------------------------------------------------------------------


def test_parse_interval_and_cron():
    assert parse_interval("15m") == timedelta(minutes=15)
    assert parse_interval("1h30m") == timedelta(hours=1, minutes=30)
    assert parse_interval("0 9 * * *") is None
    assert parse_interval("15x") is None
    start = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert next_run("0 9 * * *", start) == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert next_run("2h", start) == start + timedelta(hours=2)


def test_text_filter():
    f = TextFilter(keywords=["Price", "cost"], exclude=["spam"])
    assert f.matches("what's the PRICE?")
    assert not f.matches("price spam")
    assert not f.matches("hello")
    assert TextFilter(keywords=["a", "b"], match="all").matches("A and B")
    assert not TextFilter(keywords=["a", "z"], match="all").matches("a only")
    assert TextFilter(regex=r"\bhow much\b").matches("HOW MUCH is it")
    assert TextFilter().matches(None)


def test_validation_errors_are_clear():
    bad = [
        {"name": "a", "trigger": "nope"},
        {"name": "b", "every": "soon", "trigger": "dm_autoreply", "action": {"reply": "x"}},
        {"name": "c", "trigger": "dm_autoreply"},
        {"name": "d", "trigger": {"type": "follow_from"}},
        {"name": "e", "trigger": {"type": "hashtag_engage", "tags": ["x"], "bogus": 1}},
        {"name": "e", "trigger": "unfollow_nonfollowers"},
    ]
    with pytest.raises(RulesError) as e:
        parse_rules(bad)
    msg = str(e.value)
    assert "unknown trigger 'nope'" in msg
    assert "rules[1] (b).every" in msg
    assert "rules[2] (c).action.reply: Field required" in msg
    assert "set exactly one of 'user' or 'post'" in msg
    assert "trigger.bogus" in msg
    assert "duplicate rule name 'e'" in msg


def test_example_file_is_valid():
    ruleset = load_rules(EXAMPLES)
    types = {r.type for r in ruleset.rules}
    assert types == {
        "comment_autoreply",
        "dm_autoreply",
        "welcome_dm",
        "hashtag_engage",
        "unfollow_nonfollowers",
        "follow_from",
    }
    assert all(r.limits.min_delay == 20 for r in ruleset.rules)  # defaults merged


# --- handlers ----------------------------------------------------------------

COMMENT_RULE = {
    "name": "price",
    "trigger": {"type": "comment_autoreply", "posts": 1},
    "filter": {"keywords": ["price"]},
    "action": {"reply": "Hi @{username}: {text}", "hide_blocklist": ["free followers"]},
}


def _comments(fake: FakeBackend) -> None:
    fake.comments["m1"] = [
        Comment(id="c1", media_id="m1", text="Price?", username="alice"),
        Comment(id="c2", media_id="m1", text="nice", username="bob"),
        Comment(id="c3", media_id="m1", text="get FREE followers", username="spam"),
        Comment(id="c4", media_id="m1", text="price is in bio", username="me"),
        Comment(
            id="c5",
            media_id="m1",
            text="price",
            username="old",
            created_at=datetime.now(UTC) - timedelta(days=30),
        ),
    ]


def test_comment_autoreply_replies_hides_and_dedups(home, fake):
    _comments(fake)
    [res] = run([COMMENT_RULE], fake)
    assert res.status == "ok" and res.acted == 2
    assert ("comment", "m1", "Hi @alice: Price?", "c1") in fake.calls
    assert ("hide", "c3") in fake.calls
    assert len(fake.calls) == 2
    fake.calls.clear()
    [res] = run([COMMENT_RULE], fake)
    assert res.acted == 0 and fake.calls == []


def test_dry_run_changes_nothing(home, fake):
    _comments(fake)
    ctx = make_ctx(fake, dry_run=True)
    engine = make_engine([COMMENT_RULE], ctx)
    [res] = engine.run_once()
    assert res.status == "dry-run" and res.acted == 2
    assert fake.calls == []
    assert ctx.store.recent_actions() == []
    # Not recorded on disk, so a real run is still due and still acts.
    real = make_engine([COMMENT_RULE], make_ctx(fake))
    [res] = real.run_once()
    assert res.acted == 2 and len(fake.calls) == 2


def test_dm_autoreply(home, fake):
    fake.threads = [
        DirectThread(id="t1", users=[_user("ann")], last_message="Collab?", unread=True),
        DirectThread(id="t2", users=[_user("ben")], last_message="hey", unread=True),
    ]
    rule = {
        "name": "dms",
        "trigger": "dm_autoreply",
        "filter": {"keywords": ["collab"]},
        "action": {"reply": "Thanks {username}"},
    }
    [res] = run([rule], fake)
    assert fake.calls == [("reply_dm", "t1", "Thanks ann"), ("seen", "t1")]
    [res] = run([rule], fake)
    assert res.acted == 0


def test_welcome_dm_baseline_cap_and_carryover(home, fake):
    fake.follower_list = [_user("old")]
    rule = {
        "name": "welcome",
        "trigger": "welcome_dm",
        "action": {"message": "Hi {full_name}"},
        "limits": {"max_per_run": 1},
    }
    [res] = run([rule], fake)
    assert fake.calls == [] and "baseline" in res.message
    fake.follower_list = [_user("n1", full_name="New One"), _user("n2"), _user("old")]
    [res] = run([rule], fake)
    assert fake.calls == [("send_dm", ("n1",), "Hi New One")]
    assert "queued" in res.message
    [res] = run([rule], fake)  # n2 was queued for this run
    assert fake.calls[-1] == ("send_dm", ("n2",), "Hi n2")
    [res] = run([rule], fake)
    assert len(fake.calls) == 2


def test_hashtag_engage(home, fake, monkeypatch):
    def m(i: str, owner: str, caption: str = "") -> Media:
        return Media(id=i, media_type=MediaType.IMAGE, owner_username=owner, caption=caption)

    fake.tag_media = {
        "cats": [m("a", "x"), m("b", "me"), m("c", "y", "giveaway")],
        "dogs": [m("a", "x"), m("d", "z")],
    }
    rule = {
        "name": "tags",
        "trigger": {"type": "hashtag_engage", "tags": ["#cats", "dogs"]},
        "filter": {"exclude": ["giveaway"]},
        "action": {"like": True, "comments": ["nice #{tag} @{username}"]},
    }
    [res] = run([rule], fake)
    assert [c for c in fake.calls if c[0] == "like"] == [("like", "a"), ("like", "d")]
    assert ("comment", "a", "nice #cats @x", None) in fake.calls
    assert res.acted == 2
    fake.calls.clear()
    run([rule], fake)
    assert fake.calls == []


def test_unfollow_nonfollowers(home, fake):
    fake.follower_list = [_user("mutual")]
    fake.following_list = [_user("mutual"), _user("vip"), _user("recent"), _user("ghost")]
    ctx = make_ctx(fake)
    ctx.store.log_action("test", "follow", "recent")
    with ctx.store.tx() as c:  # 'ghost' was followed by igctl 10 days ago
        c.execute(
            "INSERT INTO actions(account, action, target, ok, detail, at) VALUES (?,?,?,?,?,?)",
            ("test", "follow", "ghost", 1, "", iso(datetime.now(UTC) - timedelta(days=10))),
        )
    rule = {
        "name": "prune",
        "trigger": {"type": "unfollow_nonfollowers", "whitelist": ["@VIP"], "min_days_followed": 7},
    }
    [res] = make_engine([rule], ctx).run_once(force=True)
    assert fake.calls == [("unfollow", "ghost")]
    assert res.acted == 1


def test_follow_from_followers(home, fake):
    fake.following_list = [_user("already")]
    fake.follower_list = [
        _user("already"),
        _user("private", is_private=True),
        _user("me"),
        _user("fresh"),
        _user("fresh2"),
    ]
    rule = {
        "name": "ff",
        "trigger": {"type": "follow_from", "user": "@brand", "skip_private": True},
        "limits": {"max_per_run": 1},
    }
    [res] = run([rule], fake)
    assert fake.calls == [("follow", "fresh")]
    run([rule], fake)  # never re-follows; moves on to the next candidate
    assert fake.calls == [("follow", "fresh"), ("follow", "fresh2")]


def test_follow_from_likers_unsupported(home, fake):
    rule = {"name": "lk", "trigger": {"type": "follow_from", "post": "m1", "audience": "likers"}}
    [res] = run([rule], fake)
    assert res.status == "unsupported" and "media_likers" in res.message


# --- engine ------------------------------------------------------------------


def test_schedule_and_daily_cap(home, fake):
    _comments(fake)
    ctx = make_ctx(fake)
    rule = {**COMMENT_RULE, "every": "1h"}
    engine = make_engine([rule], ctx)
    assert len(engine.run_once()) == 1  # never ran -> due
    assert engine.run_once() == []  # ran just now
    assert engine.next_due(engine.ruleset.rules[0]) > datetime.now(UTC)

    ctx.account.limits.daily["like"] = 1
    fake.tag_media = {"t": [Media(id=i, media_type=MediaType.IMAGE) for i in "xyz"]}
    tag_rule = {"name": "t", "trigger": {"type": "hashtag_engage", "tags": ["t"]}}
    [res] = make_engine([tag_rule], ctx).run_once()
    assert res.status == "capped" and res.acted == 1


def test_disabled_rules_skipped_unless_named(home, fake):
    rules = [{**COMMENT_RULE, "enabled": False}]
    _comments(fake)
    engine = make_engine(rules, make_ctx(fake))
    assert engine.run_once() == []
    assert len(engine.run_once("price")) == 1


def test_loop_runs_cycles(home, fake):
    engine = make_engine([{**COMMENT_RULE, "every": "10m"}], make_ctx(fake))
    seen, waits = [], []
    engine.loop(on_results=seen.append, max_cycles=2, wait=waits.append)
    assert len(seen) == 1  # second cycle: nothing due
    assert len(waits) == 1 and 1 <= waits[0] <= 60


# --- CLI ---------------------------------------------------------------------


def test_cli_commands(home, fake, tmp_path):
    runner = CliRunner()
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.safe_dump([COMMENT_RULE]), encoding="utf-8")
    _comments(fake)

    res = runner.invoke(automate_app, ["validate", str(EXAMPLES)])
    assert res.exit_code == 0, res.output
    res = runner.invoke(automate_app, ["list-triggers"])
    assert res.exit_code == 0 and "welcome_dm" in res.output

    res = runner.invoke(automate_app, ["run", str(rules_file)], obj=make_ctx(fake))
    assert res.exit_code == 0, res.output
    assert len(fake.calls) == 2

    res = runner.invoke(automate_app, ["log", "-n", "5"], obj=make_ctx(fake))
    assert res.exit_code == 0 and "hide_comment" in res.output

    bad = tmp_path / "bad.yaml"
    bad.write_text("- name: x\n  trigger: nope\n", encoding="utf-8")
    res = runner.invoke(automate_app, ["validate", str(bad)])
    assert res.exit_code != 0 and isinstance(res.exception, RulesError)


# --- regressions -------------------------------------------------------------


def test_auth_error_stops_rule_and_account(home, fake):
    from igctl.backends.base import AuthError

    calls = []

    def dead(username: str) -> None:
        calls.append(username)
        raise AuthError("session expired")

    fake.follow = dead  # type: ignore[method-assign]
    fake.follower_list = [_user("a"), _user("b"), _user("c")]
    rules = [
        {"name": "f1", "trigger": {"type": "follow_from", "user": "src"}},
        {"name": "f2", "trigger": {"type": "follow_from", "user": "src2"}},
    ]
    r1, r2 = run(rules, fake)
    assert calls == ["a"]  # no more requests with a dead session
    assert r1.status == "error" and "session expired" in r1.message
    assert r2.status == "halted"


def test_action_block_halts_account_across_runs(home, fake):
    from igctl.backends.base import ActionBlocked

    def blocked(media_id: str) -> None:
        raise ActionBlocked("feedback_required")

    fake.like = blocked  # type: ignore[method-assign]
    fake.tag_media = {"cats": [Media(id="a", media_type=MediaType.IMAGE, owner_username="x")]}
    rule = {"name": "tags", "trigger": {"type": "hashtag_engage", "tags": ["cats"]}}
    [res] = run([rule], fake)
    assert res.status == "blocked"
    # A fresh engine (e.g. the next cron run) still respects the persisted 24h cooldown.
    [res] = run([rule], fake)
    assert res.status == "halted" and "feedback_required" in res.message


def test_dm_autoreply_never_answers_itself(home, fake):
    rule = {
        "name": "dms",
        "trigger": {"type": "dm_autoreply", "unread_only": False, "once_per_thread": False},
        "action": {"reply": "Thanks {username}"},
    }
    fake.threads = [
        DirectThread(
            id="t1", users=[_user("ann")], last_message="hi", last_message_id="m1",
            last_from_me=False,
        ),
        DirectThread(id="t2", users=[_user("ben")], last_message="my dm", last_from_me=True),
        DirectThread(id="t3", users=[_user("cy")], last_message="x", last_sender_id="me-id"),
    ]  # fmt: skip
    run([rule], fake)
    assert [c[1] for c in fake.calls if c[0] == "reply_dm"] == ["t1"]
    # Our reply is now the newest message (and bumped the activity time): no new reply.
    fake.threads = [
        DirectThread(
            id="t1", users=[_user("ann")], last_message="Thanks ann", last_message_id="m2",
            last_from_me=True, last_activity_at=datetime.now(UTC),
        )
    ]  # fmt: skip
    fake.calls.clear()
    run([rule], fake)
    assert fake.calls == []


def test_welcome_dm_ignores_old_followers_sliding_into_window(home, fake):
    rule = {
        "name": "welcome",
        "trigger": {"type": "welcome_dm", "scan_limit": 2},
        "action": {"message": "Hi"},
    }
    fake.follower_list = [_user("b"), _user("a"), _user("old")]
    run([rule], fake)  # baseline: window [b, a]
    fake.follower_list = [_user("new"), _user("b"), _user("old")]  # 'a' unfollowed
    run([rule], fake)
    assert fake.calls == [("send_dm", ("new",), "Hi")]
    fake.follower_list = [_user("b"), _user("old")]  # 'new' left; 'old' slides into window
    fake.calls.clear()
    run([rule], fake)
    assert fake.calls == []
    ctx = make_ctx(fake)
    rows = ctx.store._conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE kind='automation:followers'"
    ).fetchone()[0]
    assert rows == 1  # state is upserted, not appended every run


def test_welcome_dm_retries_failed_dms(home, fake):
    attempts = []

    def flaky(usernames: list[str], text: str) -> DirectMessage:
        attempts.append(usernames[0])
        if len(attempts) == 1:
            raise RuntimeError("network blip")
        return DirectMessage(id="dm", thread_id="t")

    rule = {"name": "welcome", "trigger": "welcome_dm", "action": {"message": "Hi"}}
    fake.follower_list = [_user("old")]
    run([rule], fake)
    fake.send_dm = flaky  # type: ignore[method-assign]
    fake.follower_list = [_user("new"), _user("old")]
    [res] = run([rule], fake)
    assert res.errors == 1 and "queued" in res.message
    run([rule], fake)
    assert attempts == ["new", "new"]
    run([rule], fake)
    assert attempts == ["new", "new"]  # delivered; not sent again


def test_unfollow_min_days_skips_untracked_follows(home, fake):
    fake.following_list = [_user("manual"), _user("JohnDoe")]
    ctx = make_ctx(fake)
    with ctx.store.tx() as c:  # logged with user-supplied case 10 days ago
        c.execute(
            "INSERT INTO actions(account, action, target, ok, detail, at) VALUES (?,?,?,?,?,?)",
            ("test", "follow", "JOHNDOE", 1, "", iso(datetime.now(UTC) - timedelta(days=10))),
        )
    rule = {
        "name": "prune",
        "trigger": {"type": "unfollow_nonfollowers", "min_days_followed": 7},
    }
    make_engine([rule], ctx).run_once(force=True)
    # 'manual' has no follow record, so it may have been followed minutes ago: kept.
    assert fake.calls == [("unfollow", "JohnDoe")]


def test_dry_run_reports_unsupported_actions(home, fake):
    class NoDm(FakeBackend):
        send_dm = Backend.send_dm  # type: ignore[assignment]

    nodm = NoDm()
    nodm.follower_list = [_user("old")]
    rule = {"name": "welcome", "trigger": "welcome_dm", "action": {"message": "Hi"}}
    run([rule], nodm)
    nodm.follower_list = [_user("new"), _user("old")]
    [res] = run([rule], nodm, dry_run=True)
    assert res.status == "unsupported"


def test_cli_run_nothing_due_prints_json(home, fake, tmp_path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.safe_dump([COMMENT_RULE]), encoding="utf-8")
    ctx = make_ctx(fake)
    make_engine([COMMENT_RULE], ctx).run_once()  # now it's not due
    ctx.json_output = True
    res = CliRunner().invoke(automate_app, ["run", str(rules_file)], obj=ctx)
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == "[]"
