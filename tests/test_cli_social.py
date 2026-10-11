from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from igctl.backends.base import Backend, NotSupported, RateLimited
from igctl.cli import dm, engage
from igctl.cli.engage import (
    normalize_username,
    pk_to_shortcode,
    resolve_media,
    shortcode_from_url,
    shortcode_to_pk,
)
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
from igctl.core.ratelimit import Guard


class FakeBackend(Backend):
    name = "private"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()
        self.comments_data = [
            Comment(id="c1", media_id="1", text="Buy followers now!", username="spam"),
            Comment(id="c2", media_id="1", text="Lovely photo", username="fan"),
            Comment(id="c3", media_id="1", text="FREE followers here", username="spam2"),
        ]
        self.following_data = [UserSummary(id=str(i), username=f"u{i}") for i in range(1, 6)]
        self.followers_data = [
            UserSummary(id="1", username="u1"),
            UserSummary(id="3", username="u3"),
        ]

    def _rec(self, *call) -> None:
        if call[1] in self.fail_on:
            raise RuntimeError(f"boom {call[1]}")
        self.calls.append(call)

    def me(self) -> Profile:
        return Profile(id="0", username="me")

    def publish(self, req: PublishRequest) -> PublishResult:
        raise NotSupported(self.name, "publish")

    def get_profile(self, username: str) -> Profile:
        return Profile(id="9", username=username, full_name=f"{username.title()} Smith")

    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        return []

    def list_comments(self, media_id: str, limit: int = 100) -> list[Comment]:
        return self.comments_data[:limit]

    def followers(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        yield from self.followers_data

    def following(self, username: str | None = None, limit: int = 0) -> Iterator[UserSummary]:
        yield from self.following_data

    def hashtag_media(self, tag: str, limit: int = 50, recent: bool = False) -> list[Media]:
        return [Media(id=str(100 + i), media_type=MediaType.IMAGE) for i in range(limit)]

    def search_users(self, query: str, limit: int = 20) -> list[UserSummary]:
        return [UserSummary(id="7", username=query)]

    def comment(self, media_id: str, text: str, reply_to: str | None = None) -> Comment:
        self._rec("comment", media_id, text, reply_to)
        return Comment(id="new", media_id=media_id, text=text, username="me")

    def delete_comment(self, media_id: str, comment_id: str) -> None:
        self._rec("delete_comment", comment_id)

    def hide_comment(self, comment_id: str, hide: bool = True) -> None:
        self._rec("hide_comment", comment_id, hide)

    def like(self, media_id: str) -> None:
        self._rec("like", media_id)

    def unlike(self, media_id: str) -> None:
        self._rec("unlike", media_id)

    def follow(self, username: str) -> None:
        self._rec("follow", username)

    def unfollow(self, username: str) -> None:
        self._rec("unfollow", username)

    def list_threads(self, limit: int = 20, unread_only: bool = False) -> list[DirectThread]:
        t = DirectThread(id="t1", users=[UserSummary(id="5", username="bob")], unread=True)
        return [t] if unread_only else [t, DirectThread(id="t2", title="Team")]

    def thread_messages(self, thread_id: str, limit: int = 20) -> list[DirectMessage]:
        return [DirectMessage(id="m1", thread_id=thread_id, text="hello")]

    def send_dm(self, usernames: list[str], text: str) -> DirectMessage:
        self._rec("send_dm", ",".join(usernames), text)
        return DirectMessage(id="m", thread_id="t", text=text)

    def reply_dm(self, thread_id: str, text: str) -> DirectMessage:
        self._rec("reply_dm", thread_id, text)
        return DirectMessage(id="m", thread_id=thread_id, text=text)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))
    limits = Limits(min_delay=0, max_delay=0, daily={"like": 3, "follow": 50, "dm": 50})
    Config(
        default_account="t", accounts={"t": Account(name="t", backend="private", limits=limits)}
    ).save()
    backend = FakeBackend()
    state: dict = {"dry_run": False, "json": False}

    root = typer.Typer()

    @root.callback()
    def _cb(ctx: typer.Context) -> None:
        c = Context(dry_run=state["dry_run"], json_output=state["json"])
        c._backends["private"] = backend
        c.__dict__["guard"] = Guard("t", limits, c.store, dry_run=c.dry_run, sleep=lambda s: None)
        ctx.obj = c
        state["ctx"] = c

    root.add_typer(engage.app, name="engage")
    root.add_typer(dm.app, name="dm")
    runner = CliRunner()

    def run(*args: str, input: str | None = None):
        return runner.invoke(root, list(args), input=input, catch_exceptions=False)

    return run, backend, state, tmp_path


# --- shortcode / reference resolution ---------------------------------------------------


@pytest.mark.parametrize(
    "code,pk",
    [("B1LbfVPlwIA", 2110901750722920960), ("B-fKL9qpeab", 2278584739065882267)],
)
def test_shortcode_roundtrip(code: str, pk: int) -> None:
    assert shortcode_to_pk(code) == pk
    assert pk_to_shortcode(pk) == code


def test_shortcode_private_suffix_ignored() -> None:
    assert shortcode_to_pk("CCQQsCXjOaBfS3I2PpqsNkxElV9DXj61vzo5xs0") == 2346448800803776129


def test_shortcode_invalid() -> None:
    with pytest.raises(ValueError):
        shortcode_to_pk("bad!code")
    with pytest.raises(ValueError):
        shortcode_from_url("https://instagram.com/someuser/")


def test_resolve_media_forms() -> None:
    b = FakeBackend()
    assert resolve_media(b, "12345") == "12345"
    assert resolve_media(b, "12345_678") == "12345_678"
    assert resolve_media(b, "B1LbfVPlwIA") == "2110901750722920960"
    for url in (
        "https://www.instagram.com/p/B1LbfVPlwIA/?igshid=x",
        "instagram.com/reel/B1LbfVPlwIA",
        "https://instagram.com/tv/B1LbfVPlwIA/",
    ):
        assert resolve_media(b, url) == "2110901750722920960"


def test_resolve_media_prefers_backend_helper() -> None:
    b = FakeBackend()
    b.media_pk_from_url = lambda url: "999"  # type: ignore[attr-defined]
    assert resolve_media(b, "https://instagram.com/p/B1LbfVPlwIA/") == "999"


def test_normalize_username() -> None:
    assert normalize_username("@alice") == "alice"
    assert normalize_username("https://www.instagram.com/bob/") == "bob"
    assert normalize_username(" carol ") == "carol"


# --- engage -----------------------------------------------------------------------------


def test_like_multiple_logs_actions(env) -> None:
    run, b, state, _ = env
    r = run("engage", "like", "111", "https://instagram.com/p/B1LbfVPlwIA/")
    assert r.exit_code == 0, r.output
    assert b.calls == [("like", "111"), ("like", "2110901750722920960")]
    assert state["ctx"].guard.used("like") == 2


def test_like_dry_run_does_nothing(env) -> None:
    run, b, state, _ = env
    state["dry_run"] = True
    r = run("engage", "like", "111")
    assert r.exit_code == 0
    assert b.calls == []


def test_comment_with_reply(env) -> None:
    run, b, _, _ = env
    r = run("engage", "comment", "5", "nice!", "--reply-to", "c9")
    assert r.exit_code == 0, r.output
    assert b.calls == [("comment", "5", "nice!", "c9")]


def test_comments_list_json(env) -> None:
    run, _, state, _ = env
    state["json"] = True
    r = run("engage", "comments", "1", "--limit", "2")
    assert [c["id"] for c in json.loads(r.stdout)] == ["c1", "c2"]


def test_hide_and_delete_comment(env) -> None:
    run, b, _, _ = env
    assert run("engage", "hide-comment", "c1", "--unhide").exit_code == 0
    assert run("engage", "delete-comment", "1", "c2").exit_code == 0
    assert b.calls == [("hide_comment", "c1", False), ("delete_comment", "c2")]


def test_purge_comments_requires_confirmation(env) -> None:
    run, b, _, _ = env
    r = run("engage", "purge-comments", "1", "--match", "followers", "-i", input="n\n")
    assert r.exit_code != 0
    assert b.calls == []
    r = run("engage", "purge-comments", "1", "--match", "followers", "-i", "--yes")
    assert r.exit_code == 0, r.output
    assert b.calls == [("delete_comment", "c1"), ("delete_comment", "c3")]


def test_purge_comments_hide(env) -> None:
    run, b, _, _ = env
    r = run("engage", "purge-comments", "1", "-m", "FREE", "--hide", "-y")
    assert r.exit_code == 0
    assert b.calls == [("hide_comment", "c3", True)]


def test_follow_unfollow_normalizes(env) -> None:
    run, b, _, _ = env
    run("engage", "follow", "@alice", "instagram.com/bob")
    run("engage", "unfollow", "carol")
    assert b.calls == [("follow", "alice"), ("follow", "bob"), ("unfollow", "carol")]


def test_followers_and_search_json(env) -> None:
    run, _, state, _ = env
    state["json"] = True
    r = run("engage", "following", "--limit", "2")
    assert [u["username"] for u in json.loads(r.stdout)] == ["u1", "u2"]
    r = run("engage", "search", "zed")
    assert json.loads(r.stdout)[0]["username"] == "zed"
    r = run("engage", "hashtag", "#cats", "--limit", "3")
    assert len(json.loads(r.stdout)) == 3


def test_bulk_like_stops_at_daily_cap(env) -> None:
    run, b, state, _ = env
    state["json"] = True
    r = run("engage", "bulk-like", "--hashtag", "cats", "--max", "5")
    assert r.exit_code == 1, r.output  # stopped early: not a clean success
    summary = json.loads(r.stdout)
    assert summary["done"] == 3 and summary["stopped"]
    assert len(b.calls) == 3


def test_bulk_like_stops_on_backend_rate_limit(env) -> None:
    run, b, state, _ = env
    state["json"] = True

    def limited(media_id: str) -> None:
        raise RateLimited("feedback_required")

    b.like = limited  # type: ignore[method-assign]
    r = run("engage", "bulk-like", "-t", "cats", "--max", "2")
    assert json.loads(r.stdout)["stopped"] == "feedback_required"


def test_bulk_follow_from_file_continues_after_error(env) -> None:
    run, b, state, tmp = env
    state["json"] = True
    f = tmp / "users.txt"
    f.write_text("# list\n@a\nb\n\na\nc\nd\n", encoding="utf-8")
    b.fail_on = {"b"}
    r = run("engage", "bulk-follow", "--from-file", str(f), "--max", "3")
    summary = json.loads(r.stdout)
    assert (summary["done"], summary["failed"]) == (2, 1)
    assert b.calls == [("follow", "a"), ("follow", "c")]


def test_bulk_unfollow_non_followers_with_whitelist(env) -> None:
    run, b, _, tmp = env
    wl = tmp / "keep.txt"
    wl.write_text("U4\n", encoding="utf-8")
    r = run(
        "engage", "bulk-unfollow", "--non-followers", "--whitelist", str(wl), "--max", "5", "-y"
    )
    assert r.exit_code == 0, r.output
    assert b.calls == [("unfollow", "u2"), ("unfollow", "u5")]


def test_bulk_unfollow_aborts_without_confirmation(env) -> None:
    run, b, _, _ = env
    r = run("engage", "bulk-unfollow", "--max", "5", input="n\n")
    assert r.exit_code != 0
    assert b.calls == []


# --- dm ---------------------------------------------------------------------------------


def test_inbox_and_read(env) -> None:
    run, _, state, _ = env
    r = run("dm", "inbox", "--unread")
    assert r.exit_code == 0 and "t1" in r.stdout
    state["json"] = True
    r = run("dm", "read", "t1")
    assert json.loads(r.stdout)[0]["text"] == "hello"


def test_send_individual_and_group(env) -> None:
    run, b, _, tmp = env
    assert run("dm", "send", "a", "@b", "--text", "hi").exit_code == 0
    msg = tmp / "m.txt"
    msg.write_text("from file\n", encoding="utf-8")
    assert run("dm", "send", "a", "b", "--file", str(msg), "--group").exit_code == 0
    assert b.calls == [
        ("send_dm", "a", "hi"),
        ("send_dm", "b", "hi"),
        ("send_dm", "a,b", "from file"),
    ]


def test_send_requires_one_body(env) -> None:
    run, _, _, _ = env
    assert run("dm", "send", "a").exit_code != 0


def test_reply(env) -> None:
    run, b, _, _ = env
    assert run("dm", "reply", "t1", "thanks").exit_code == 0
    assert b.calls == [("reply_dm", "t1", "thanks")]


def test_broadcast_dedups_across_runs(env) -> None:
    run, b, state, tmp = env
    f = tmp / "users.txt"
    f.write_text("alice\nbob\ncarol\n", encoding="utf-8")
    args = ("dm", "broadcast", "-f", str(f), "-t", "Hi {first_name} (@{username})", "--yes")
    r = run(*args, "--max", "2")
    assert r.exit_code == 0, r.output
    assert b.calls == [
        ("send_dm", "alice", "Hi Alice (@alice)"),
        ("send_dm", "bob", "Hi Bob (@bob)"),
    ]
    state["json"] = True
    r = run(*args)
    summary = json.loads(r.stdout)
    assert (summary["done"], summary["skipped"]) == (1, 2)
    assert b.calls[-1] == ("send_dm", "carol", "Hi Carol (@carol)")
    assert state["ctx"].guard.used("dm") == 3


def test_broadcast_dry_run_does_not_mark_seen(env) -> None:
    run, b, state, tmp = env
    f = tmp / "users.txt"
    f.write_text("alice\n", encoding="utf-8")
    state["dry_run"] = True
    assert run("dm", "broadcast", "-f", str(f), "-t", "Hi {username}").exit_code == 0
    state["dry_run"] = False
    assert run("dm", "broadcast", "-f", str(f), "-t", "Hi {username}", "-y").exit_code == 0
    assert b.calls == [("send_dm", "alice", "Hi alice")]


def test_broadcast_rejects_unknown_placeholder(env) -> None:
    run, _, _, tmp = env
    f = tmp / "users.txt"
    f.write_text("alice\n", encoding="utf-8")
    r = run("dm", "broadcast", "-f", str(f), "-t", "Hi {nickname}", "-y")
    assert r.exit_code != 0


# --- regressions -------------------------------------------------------------------


def test_dry_run_reports_unsupported_backend_action(tmp_path: Path, monkeypatch) -> None:
    from functools import partial

    from igctl.backends.base import NotSupported
    from igctl.backends.graph import GraphBackend
    from igctl.core.ratelimit import requires
    from igctl.core.storage import Store

    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))
    graph = GraphBackend(Account(name="g", ig_user_id="1"))
    guard = Guard("g", Limits(), Store(tmp_path / "x.db"), dry_run=True)
    with pytest.raises(NotSupported, match="like"):
        guard.act("like", "1", partial(graph.like, "1"))
    with pytest.raises(NotSupported, match="send_dm"):
        guard.act("dm", "bob", requires(graph, "send_dm", lambda: graph.send_dm(["bob"], "x")))
    assert guard.act("comment", "1", partial(graph.comment, "1", "hi")) is None  # supported


def test_dry_run_counts_toward_daily_cap(env) -> None:
    run, b, state, _ = env
    state["dry_run"], state["json"] = True, True
    r = run("engage", "bulk-like", "--hashtag", "cats", "--max", "5")  # cap: 3 likes/24h
    summary = json.loads(r.stdout)
    assert summary["done"] == 3 and summary["stopped"]  # what a real run would do
    assert b.calls == []


def test_read_lines_handles_windows_encodings(tmp_path: Path) -> None:
    from igctl.backends.base import IgctlError
    from igctl.cli.engage import read_lines

    bom = tmp_path / "bom.txt"
    bom.write_bytes("﻿Alice\nbob\n".encode())
    assert read_lines(bom) == ["alice", "bob"]  # BOM stripped, handles lower-cased
    utf16 = tmp_path / "u16.txt"
    utf16.write_text("carol\r\ndave\r\n", encoding="utf-16")
    assert read_lines(utf16) == ["carol", "dave"]
    cp = tmp_path / "cp.txt"
    cp.write_bytes("caf\xe9\n".encode("cp1252"))
    with pytest.raises(IgctlError, match="not UTF-8"):
        read_lines(cp)
    with pytest.raises(IgctlError, match="Can't read"):
        read_lines(tmp_path / "missing.txt")


def test_bulk_failures_exit_nonzero(env) -> None:
    run, b, state, tmp = env
    users = tmp / "users.txt"
    users.write_text("ok1\nbad\n", encoding="utf-8")

    def follow(username: str) -> None:
        if username == "bad":
            raise RuntimeError("nope")

    b.follow = follow  # type: ignore[method-assign]
    r = run("engage", "bulk-follow", "-f", str(users))
    assert r.exit_code == 1


def test_json_mode_always_prints_json(env) -> None:
    run, b, state, tmp = env
    state["json"] = True
    r = run("engage", "purge-comments", "m1", "--match", "zzz-no-match", "-y")
    assert json.loads(r.stdout)["total"] == 0
    r = run("engage", "hide-comment", "c1")
    assert json.loads(r.stdout)["ok"] is True


def test_closed_pipe_exits_quietly(monkeypatch) -> None:
    import errno

    from igctl.cli import main

    def broken() -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(main, "app", broken)
    monkeypatch.setattr(main, "_silence_stdout", lambda: None)
    with pytest.raises(SystemExit) as info:
        main.run()
    assert info.value.code == 1

    def other() -> None:
        raise OSError(errno.ENOENT, "missing")

    monkeypatch.setattr(main, "app", other)
    with pytest.raises(OSError):
        main.run()
