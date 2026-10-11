from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from instagrapi import exceptions as ig
from instagrapi import types as igt

from igctl.backends.base import AuthError, IgctlError, RateLimited
from igctl.backends.private import (
    PrivateBackend,
    flatten_insights,
    map_error,
    parse_usertag,
    to_comment,
    to_media,
    to_profile,
    to_thread,
)
from igctl.core.config import Account
from igctl.core.models import MediaType, PublishRequest

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def short(pk: str = "1", username: str = "alice") -> igt.UserShort:
    return igt.UserShort(pk=pk, username=username, full_name="Alice", is_private=False)


def ig_user(pk: str = "1", username: str = "alice") -> igt.User:
    return igt.User(
        pk=pk,
        username=username,
        full_name="Alice A",
        is_private=False,
        profile_pic_url="https://cdn.example/p.jpg",
        is_verified=True,
        media_count=3,
        follower_count=100,
        following_count=50,
        biography="hi",
        is_business=True,
        category_name="Artist",
    )


def ig_media(media_type: int = 1, product_type: str = "feed", **kw) -> igt.Media:
    data = dict(
        pk="111",
        id="111_1",
        code="ABCdef",
        taken_at=NOW,
        media_type=media_type,
        product_type=product_type,
        thumbnail_url="https://cdn.example/t.jpg",
        user=short(),
        like_count=7,
        comment_count=2,
        caption_text="sunset #beach #travel",
        usertags=[],
        sponsor_tags=[],
    )
    data.update(kw)
    return igt.Media(**data)


@pytest.fixture(autouse=True)
def igctl_home(tmp_path, monkeypatch):
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))
    monkeypatch.delenv("IGCTL_ACME_PASSWORD", raising=False)
    monkeypatch.setattr("igctl.backends.private.get_secret", lambda acct, kind: None)
    return tmp_path


@pytest.fixture
def client() -> MagicMock:
    c = MagicMock()
    c.user_id = 42
    c.delay_range = None
    c.media_pk.side_effect = lambda mid: str(mid).split("_")[0]
    return c


@pytest.fixture
def backend(client) -> PrivateBackend:
    return PrivateBackend(Account(name="acme", backend="private", username="acme"), client=client)


# ---------------------------------------------------------------- mappers
def test_to_profile():
    p = to_profile(ig_user())
    assert p.id == "1" and p.username == "alice"
    assert p.followers_count == 100 and p.following_count == 50 and p.media_count == 3
    assert p.is_business and p.is_verified and p.category == "Artist"
    assert p.profile_pic_url == "https://cdn.example/p.jpg"


@pytest.mark.parametrize(
    ("media_type", "product_type", "expected", "segment"),
    [
        (1, "feed", MediaType.IMAGE, "/p/"),
        (2, "feed", MediaType.VIDEO, "/p/"),
        (2, "clips", MediaType.REEL, "/reel/"),
        (8, "carousel_container", MediaType.CAROUSEL, "/p/"),
    ],
)
def test_to_media_types(media_type, product_type, expected, segment):
    m = to_media(ig_media(media_type, product_type))
    assert m.media_type is expected
    assert m.id == "111_1" and m.code == "ABCdef"
    assert segment in m.permalink
    assert m.hashtags == ["beach", "travel"]
    assert m.owner_username == "alice" and m.like_count == 7


def test_to_comment_and_thread():
    cm = igt.Comment(
        pk="9",
        text="nice",
        user=short("5", "bob"),
        created_at_utc=NOW,
        content_type="comment",
        status="Active",
        replied_to_comment_id="8",
    )
    out = to_comment(cm, "111_1")
    assert (out.id, out.username, out.user_id, out.replied_to_id) == ("9", "bob", "5", "8")

    thread = MagicMock(spec=igt.DirectThread)
    thread.id, thread.thread_title, thread.last_activity_at = "77", "Chat", NOW
    thread.users = [short("5", "bob")]
    thread.messages = [MagicMock(text="hey", item_type="text")]
    thread.is_seen.return_value = False
    t = to_thread(thread, "42")
    assert t.id == "77" and t.unread and t.last_message == "hey"
    assert t.users[0].username == "bob"


def test_flatten_insights():
    rows = flatten_insights(
        {
            "inline_insights_node": {"metrics": {"reach_count": 10, "impression_count": 20}},
            "like_count": 3,
            "has_x": True,
            "name": "skip",
        }
    )
    names = {r.name: r.value for r in rows}
    assert names == {
        "inline_insights_node.metrics.reach_count": 10,
        "inline_insights_node.metrics.impression_count": 20,
        "like_count": 3,
    }


def test_parse_usertag():
    assert parse_usertag("@bob") == ("bob", 0.5, 0.5)
    assert parse_usertag("bob:0.2,0.8") == ("bob", 0.2, 0.8)
    with pytest.raises(IgctlError):
        parse_usertag("bob:2,0")


# ---------------------------------------------------------------- publish
@pytest.fixture
def files(tmp_path) -> dict[str, Path]:
    out = {}
    for name in ("a.jpg", "b.jpg", "v.mp4", "cover.jpg"):
        (tmp_path / name).write_bytes(b"x")
        out[name] = tmp_path / name
    return out


def test_publish_image_with_location_and_tags(backend, client, files):
    client.photo_upload.return_value = ig_media(1)
    client.fbsearch_places.return_value = [igt.Location(pk=1, name="Paris")]
    client.user_info_by_username_v1.return_value = ig_user("5", "bob")
    res = backend.publish(
        PublishRequest(
            media_type=MediaType.IMAGE,
            paths=[files["a.jpg"]],
            caption="hello",
            location="Paris",
            user_tags=["bob"],
        )
    )
    assert res.media_id == "111_1" and res.backend == "private"
    args, kwargs = client.photo_upload.call_args
    assert args == (files["a.jpg"], "hello")
    assert kwargs["location"].name == "Paris"
    assert kwargs["usertags"][0].user.username == "bob"


def test_publish_reel_passes_cover_and_feed_flag(backend, client, files):
    client.clip_upload.return_value = ig_media(2, "clips")
    res = backend.publish(
        PublishRequest(
            media_type=MediaType.REEL,
            paths=[files["v.mp4"]],
            cover=files["cover.jpg"],
            share_to_feed=False,
        )
    )
    kwargs = client.clip_upload.call_args.kwargs
    assert kwargs["thumbnail"] == files["cover.jpg"]
    assert kwargs["show_preview_in_feed"] is False
    assert "/reel/" in res.permalink


def test_publish_video_and_carousel(backend, client, files):
    client.video_upload.return_value = ig_media(2)
    backend.publish(PublishRequest(media_type=MediaType.VIDEO, paths=[files["v.mp4"]]))
    client.video_upload.assert_called_once()

    client.album_upload.return_value = ig_media(8)
    backend.publish(
        PublishRequest(media_type=MediaType.CAROUSEL, paths=[files["a.jpg"], files["b.jpg"]])
    )
    assert client.album_upload.call_args.args[0] == [files["a.jpg"], files["b.jpg"]]

    with pytest.raises(IgctlError, match="at least 2"):
        backend.publish(PublishRequest(media_type=MediaType.CAROUSEL, paths=[files["a.jpg"]]))


def test_publish_story_dispatches_by_extension(backend, client, files):
    story = MagicMock(
        id="333_42",
        pk="333",
        code="c",
        taken_at=NOW,
        video_url=None,
        thumbnail_url="https://cdn.example/s.jpg",
        hashtags=[],
    )
    story.user.username = "acme"
    client.photo_upload_to_story.return_value = story
    client.video_upload_to_story.return_value = story
    res = backend.publish(
        PublishRequest(media_type=MediaType.STORY, paths=[files["a.jpg"], files["v.mp4"]])
    )
    client.photo_upload_to_story.assert_called_once()
    client.video_upload_to_story.assert_called_once()
    assert res.media_id == "333_42"
    assert res.permalink == "https://www.instagram.com/stories/acme/333/"


def test_publish_missing_file(backend):
    with pytest.raises(IgctlError, match="not found"):
        backend.publish(PublishRequest(media_type=MediaType.IMAGE, paths=[Path("nope.jpg")]))


# ---------------------------------------------------------------- pagination
def test_followers_paginates_and_honors_limit(backend, client):
    pages = {
        "": ([short(str(i), f"u{i}") for i in range(3)], "c1"),
        "c1": ([short("2", "u2"), short("3", "u3"), short("4", "u4")], "c2"),
        "c2": ([short("5", "u5")], ""),
    }
    client.user_followers_v1_chunk.side_effect = lambda uid, max_amount, max_id: pages[max_id]

    assert [u.username for u in backend.followers()] == [f"u{i}" for i in range(6)]
    assert client.user_followers_v1_chunk.call_args_list[0].args == ("42",)

    client.user_followers_v1_chunk.reset_mock()
    got = list(backend.followers(limit=4))
    assert [u.id for u in got] == ["0", "1", "2", "3"]
    assert client.user_followers_v1_chunk.call_count == 2
    assert client.user_followers_v1_chunk.call_args_list[0].kwargs["max_amount"] == 4


def test_following_resolves_username(backend, client):
    client.user_info_by_username_v1.return_value = ig_user("9", "bob")
    client.user_following_v1_chunk.return_value = ([short("1")], "")
    assert len(list(backend.following("bob"))) == 1
    assert client.user_following_v1_chunk.call_args.args == ("9",)


# ---------------------------------------------------------------- errors
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ig.LoginRequired(), AuthError),
        (ig.ChallengeRequired(), AuthError),
        (ig.TwoFactorRequired(), AuthError),
        (ig.BadPassword(), AuthError),
        (ig.PleaseWaitFewMinutes(), RateLimited),
        (ig.RateLimitError(), RateLimited),
        (ig.FeedbackRequired(), RateLimited),
        (ig.ClientThrottledError(), RateLimited),
        (ig.UserNotFound(), IgctlError),
        (ig.ClientError("boom"), IgctlError),
    ],
)
def test_map_error(exc, expected):
    err = map_error(exc)
    assert type(err) is expected
    assert str(err)


def test_methods_translate_errors(backend, client):
    client.media_like.side_effect = ig.FeedbackRequired(message="feedback_required")
    with pytest.raises(RateLimited) as info:
        backend.like("111_1")
    assert isinstance(info.value.__cause__, ig.FeedbackRequired)

    client.user_followers_v1_chunk.side_effect = ig.LoginRequired()
    with pytest.raises(AuthError):
        list(backend.followers())


# ---------------------------------------------------------------- engagement / dm
def test_engagement_and_dm(backend, client):
    client.user_info_by_username_v1.return_value = ig_user("9", "bob")
    backend.follow("@bob")
    client.user_follow.assert_called_once_with("9")

    client.media_comment.return_value = igt.Comment(
        pk="1", text="hi", user=short(), created_at_utc=NOW, content_type="c", status="ok"
    )
    c = backend.comment("111_1", "hi", reply_to="5")
    assert c.media_id == "111_1"
    assert client.media_comment.call_args.kwargs["replied_to_comment_id"] == 5

    client.direct_send.return_value = igt.DirectMessage(
        id="m1", thread_id=7, timestamp=NOW, text="yo"
    )
    msg = backend.send_dm(["bob"], "yo")
    assert client.direct_send.call_args.kwargs["user_ids"] == [9]
    assert (msg.id, msg.thread_id) == ("m1", "7")

    backend.delete_comment("111_1", "12")
    client.comment_bulk_delete.assert_called_once_with("111_1", [12])


def test_media_ref_resolution(backend, client):
    client.media_pk_from_url.return_value = "555"
    backend.like("https://www.instagram.com/p/ABCdef/")
    client.media_like.assert_called_with("555")
    client.media_pk_from_code.return_value = "556"
    backend.like("ABCdef")
    client.media_like.assert_called_with("556")


def test_download_dispatch(backend, client, tmp_path):
    client.media_info.return_value = ig_media(2, "clips")
    client.clip_download.return_value = tmp_path / "x.mp4"
    assert backend.download_media("111_1", str(tmp_path / "out")) == [str(tmp_path / "x.mp4")]
    client.clip_download.assert_called_once()

    client.media_info.return_value = ig_media(8)
    client.album_download.return_value = [tmp_path / "1.jpg", tmp_path / "2.jpg"]
    assert len(backend.download_media("111_1", str(tmp_path))) == 2


# ---------------------------------------------------------------- session
def _fresh_client() -> MagicMock:
    c = MagicMock()
    c.user_id = None
    c.get_settings.return_value = {"uuids": {"uuid": "dev-1"}}
    return c


def test_login_reuses_saved_session(igctl_home):
    sess = igctl_home / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "acme.json").write_text(json.dumps({"uuids": {}}))
    c = _fresh_client()
    c.user_info_v1.return_value = ig_user("42", "acme")
    b = PrivateBackend(Account(name="acme", backend="private"), client=c)

    profile = b.login()
    assert profile.username == "acme"
    c.load_settings.assert_called_once_with(sess / "acme.json")
    c.get_timeline_feed.assert_called_once()
    c.login.assert_not_called()
    c.dump_settings.assert_called_once_with(sess / "acme.json")


def test_login_relogs_preserving_uuids_on_expired_session(igctl_home):
    sess = igctl_home / "sessions"
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "acme.json").write_text("{}")
    c = _fresh_client()
    c.get_timeline_feed.side_effect = ig.LoginRequired()
    c.user_info_v1.return_value = ig_user("42", "acme")
    b = PrivateBackend(Account(name="acme", backend="private", username="acme"), client=c)

    b.login(password="pw", verification_code="123456")
    c.set_settings.assert_called_once_with({})
    c.set_uuids.assert_called_once_with({"uuid": "dev-1"})
    c.login.assert_called_once_with("acme", "pw", verification_code="123456")
    c.dump_settings.assert_called_once()


def test_login_without_session_or_password_fails():
    b = PrivateBackend(
        Account(name="acme", backend="private", username="acme"), client=_fresh_client()
    )
    with pytest.raises(AuthError, match="auth login"):
        b.login()


def test_client_configured(client):
    b = PrivateBackend(Account(name="acme", backend="private", proxy="http://p:1"), client=client)
    client.set_proxy.assert_called_once_with("http://p:1")
    assert b.client.delay_range == [1, 3]
    assert callable(b.client.challenge_code_handler)
