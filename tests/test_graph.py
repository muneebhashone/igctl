from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from igctl.backends.base import AuthError, IgctlError, NotSupported, RateLimited
from igctl.backends.graph import GraphBackend
from igctl.core.config import Account, Config
from igctl.core.media_host import is_video, resolve_urls
from igctl.core.models import MediaType, PublishRequest

BASE = "https://graph.facebook.com/v23.0"
IG = "1789"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))
    monkeypatch.setenv("IGCTL_BRAND_TOKEN", "tok")


def make(**account) -> tuple[GraphBackend, list[float]]:
    sleeps: list[float] = []
    acct = Account(name="brand", ig_user_id=IG, username="mybrand", **account)
    backend = GraphBackend(
        acct, httpx.Client(), Config(), poll_interval=1.0, poll_timeout=60, sleep=sleeps.append
    )
    return backend, sleeps


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@respx.mock
def test_publish_image():
    g, _ = make()
    create = respx.post(f"{BASE}/{IG}/media").respond(json={"id": "c1"})
    status = respx.get(f"{BASE}/c1").respond(json={"status_code": "FINISHED"})
    publish = respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m1"})
    respx.get(f"{BASE}/m1").respond(json={"permalink": "https://instagram.com/p/abc/"})

    res = g.publish(
        PublishRequest(
            media_type=MediaType.IMAGE,
            urls=["https://cdn.example/a.jpg"],
            caption="hi #x",
            user_tags=["@friend"],
        )
    )

    assert res.media_id == "m1" and res.backend == "graph"
    assert res.permalink == "https://instagram.com/p/abc/"
    body = form(create.calls.last.request)
    assert body["image_url"] == "https://cdn.example/a.jpg"
    assert body["caption"] == "hi #x"
    assert json.loads(body["user_tags"]) == [{"username": "friend", "x": 0.5, "y": 0.5}]
    assert create.calls.last.request.url.params["access_token"] == "tok"
    assert form(publish.calls.last.request) == {"creation_id": "c1"}
    assert status.called  # image containers are status-checked before publishing


@respx.mock
def test_publish_carousel():
    g, _ = make()
    ids = iter(["k1", "k2", "parent"])
    create = respx.post(f"{BASE}/{IG}/media").mock(
        side_effect=lambda req: httpx.Response(200, json={"id": next(ids)})
    )
    k1 = respx.get(f"{BASE}/k1").respond(json={"status_code": "FINISHED"})
    respx.get(f"{BASE}/k2").respond(json={"status_code": "FINISHED"})
    respx.get(f"{BASE}/parent").respond(json={"status_code": "FINISHED"})
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m9"})
    respx.get(f"{BASE}/m9").respond(json={"permalink": "p"})

    res = g.publish(
        PublishRequest(
            media_type=MediaType.CAROUSEL,
            urls=["https://cdn.example/1.jpg", "https://cdn.example/2.mp4"],
            caption="album",
        )
    )

    assert res.media_id == "m9"
    assert k1.called  # image children are waited on too
    bodies = [form(c.request) for c in create.calls]
    assert bodies[0] == {"is_carousel_item": "true", "image_url": "https://cdn.example/1.jpg"}
    assert bodies[1] == {
        "is_carousel_item": "true",
        "video_url": "https://cdn.example/2.mp4",
        "media_type": "VIDEO",
    }
    assert bodies[2] == {"media_type": "CAROUSEL", "children": "k1,k2", "caption": "album"}


def test_carousel_needs_two_items():
    g, _ = make()
    with pytest.raises(IgctlError, match="carousel"):
        g.publish(PublishRequest(media_type=MediaType.CAROUSEL, urls=["https://x/1.jpg"]))


@respx.mock
def test_publish_reel_polls_until_finished():
    g, sleeps = make()
    create = respx.post(f"{BASE}/{IG}/media").respond(json={"id": "r1"})
    respx.get(f"{BASE}/r1").mock(
        side_effect=[
            httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
            httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
            httpx.Response(200, json={"status_code": "FINISHED"}),
        ]
    )
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m2"})
    respx.get(f"{BASE}/m2").respond(json={"permalink": "https://instagram.com/reel/xyz/"})

    res = g.publish(
        PublishRequest(
            media_type=MediaType.REEL, urls=["https://cdn.example/v"], share_to_feed=False
        )
    )

    assert res.media_id == "m2"
    assert sleeps == [1.0, 1.5]  # backoff between polls
    body = form(create.calls.last.request)
    assert body["media_type"] == "REELS"
    assert body["video_url"] == "https://cdn.example/v"
    assert body["share_to_feed"] == "false"


@respx.mock
def test_reel_container_error_raises():
    g, _ = make()
    respx.post(f"{BASE}/{IG}/media").respond(json={"id": "r1"})
    respx.get(f"{BASE}/r1").respond(json={"status_code": "ERROR", "status": "bad codec"})
    with pytest.raises(IgctlError, match="bad codec"):
        g.publish(PublishRequest(media_type=MediaType.REEL, urls=["https://cdn.example/v.mp4"]))


@respx.mock
def test_reel_poll_timeout():
    g, _ = make()
    g.poll_timeout = 0
    respx.post(f"{BASE}/{IG}/media").respond(json={"id": "r1"})
    respx.get(f"{BASE}/r1").respond(json={"status_code": "IN_PROGRESS"})
    with pytest.raises(IgctlError, match="Timed out"):
        g.publish(PublishRequest(media_type=MediaType.REEL, urls=["https://cdn.example/v.mp4"]))


@respx.mock
def test_story_drops_caption():
    g, _ = make()
    create = respx.post(f"{BASE}/{IG}/media").respond(json={"id": "s1"})
    respx.get(f"{BASE}/s1").respond(json={"status_code": "FINISHED"})
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m3"})
    respx.get(f"{BASE}/m3").respond(json={})
    g.publish(PublishRequest(media_type=MediaType.STORY, urls=["https://x/s.jpg"], caption="no"))
    assert form(create.calls.last.request) == {
        "media_type": "STORIES",
        "image_url": "https://x/s.jpg",
    }


@respx.mock
def test_publish_local_video_uses_resumable_upload(tmp_path: Path):
    g, _ = make()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x01\x02")
    create = respx.post(f"{BASE}/{IG}/media").respond(json={"id": "r5"})
    upload = respx.post("https://rupload.facebook.com/ig-api-upload/v23.0/r5").respond(
        json={"success": True}
    )
    respx.get(f"{BASE}/r5").respond(json={"status_code": "FINISHED"})
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m5"})
    respx.get(f"{BASE}/m5").respond(json={})

    g.publish(PublishRequest(media_type=MediaType.REEL, paths=[video]))

    body = form(create.calls.last.request)
    assert body["upload_type"] == "resumable" and body["media_type"] == "REELS"
    req = upload.calls.last.request
    assert req.headers["Authorization"] == "OAuth tok"
    assert req.headers["file_size"] == "3" and req.content == b"\x00\x01\x02"


def test_local_image_without_media_host_explains(tmp_path: Path):
    g, _ = make()
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    with pytest.raises(IgctlError, match="media_host"):
        g.publish(PublishRequest(media_type=MediaType.IMAGE, paths=[img]))


@pytest.mark.parametrize(
    ("error", "exc"),
    [
        ({"code": 190, "type": "OAuthException", "message": "expired"}, AuthError),
        ({"code": 4, "message": "Application request limit reached"}, RateLimited),
        ({"code": 32, "message": "Page request limit"}, RateLimited),
        ({"code": 9, "error_subcode": 2207042, "message": "max posts"}, RateLimited),
        ({"code": 100, "message": "Invalid parameter"}, IgctlError),
    ],
)
@respx.mock
def test_error_mapping(error, exc):
    g, _ = make()
    respx.get(f"{BASE}/{IG}").respond(400, json={"error": error})
    with pytest.raises(exc, match=error["message"]) as info:
        g.me()
    if exc is IgctlError:
        assert not isinstance(info.value, AuthError | RateLimited)


@respx.mock
def test_transient_error_is_retried():
    g, sleeps = make()
    respx.get(f"{BASE}/{IG}").mock(
        side_effect=[
            httpx.Response(500, json={"error": {"code": 2, "message": "temp"}}),
            httpx.Response(200, json={"id": IG, "username": "mybrand"}),
        ]
    )
    assert g.me().username == "mybrand"
    assert sleeps == [1]


def test_missing_token(monkeypatch):
    monkeypatch.delenv("IGCTL_BRAND_TOKEN")
    monkeypatch.setattr("igctl.core.config.keyring.get_password", lambda *a: None)
    g, _ = make()
    with pytest.raises(AuthError, match="No Graph API token"):
        _ = g.token


@respx.mock
def test_list_media_paginates():
    g, _ = make()
    nxt = f"{BASE}/{IG}/media?after=CUR&access_token=tok&limit=3"
    respx.get(f"{BASE}/{IG}/media", params={"after": "CUR"}).respond(
        json={"data": [{"id": "3", "media_type": "IMAGE"}, {"id": "4", "media_type": "IMAGE"}]}
    )
    first = respx.get(f"{BASE}/{IG}/media").respond(
        json={
            "data": [
                {
                    "id": "1",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "caption": "Go #Summer #fun",
                    "timestamp": "2025-05-01T10:00:00+0000",
                    "permalink": "https://www.instagram.com/reel/AbC/",
                },
                {"id": "2", "media_type": "CAROUSEL_ALBUM"},
            ],
            "paging": {"next": nxt},
        }
    )

    media = g.list_media(limit=3)

    assert [m.id for m in media] == ["1", "2", "3"]
    assert media[0].media_type is MediaType.REEL
    assert media[0].hashtags == ["summer", "fun"]
    assert media[0].code == "AbC"
    assert media[0].taken_at and media[0].taken_at.year == 2025
    assert media[1].media_type is MediaType.CAROUSEL
    assert first.calls[0].request.url.params["limit"] == "3"


@respx.mock
def test_business_discovery_profile_and_media():
    g, _ = make()
    route = respx.get(f"{BASE}/{IG}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "business_discovery": {
                        "id": "55",
                        "username": "nike",
                        "followers_count": 1000,
                        "follows_count": 5,
                        "media_count": 2,
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "business_discovery": {
                        "username": "nike",
                        "media": {
                            "data": [{"id": "a", "media_type": "IMAGE", "like_count": 3}],
                            "paging": {"cursors": {"after": "X"}, "next": "n"},
                        },
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "business_discovery": {
                        "username": "nike",
                        "media": {"data": [{"id": "b", "media_type": "VIDEO"}]},
                    }
                },
            ),
        ]
    )

    profile = g.get_profile("@nike")
    media = g.list_media("nike", limit=5)

    assert profile.username == "nike" and profile.followers_count == 1000
    assert profile.following_count == 5
    assert [m.id for m in media] == ["a", "b"]
    assert all(m.owner_username == "nike" for m in media)
    fields = [c.request.url.params["fields"] for c in route.calls]
    assert fields[0].startswith("business_discovery.username(nike){")
    assert "media.limit(5){" in fields[1]
    assert "media.limit(4).after(X){" in fields[2]


@respx.mock
def test_resolve_ig_user_id_instagram_host():
    acct = Account(name="brand", graph_host="graph.instagram.com")
    g = GraphBackend(acct, httpx.Client(), Config())
    respx.get("https://graph.instagram.com/v23.0/me").respond(
        json={"id": "app", "user_id": "777", "username": "me"}
    )
    assert g.ig_user_id == "777"


@respx.mock
def test_resolve_ig_user_id_facebook_host_none_linked():
    g = GraphBackend(Account(name="brand"), httpx.Client(), Config())
    respx.get(f"{BASE}/me/accounts").respond(json={"data": [{"name": "Page"}]})
    with pytest.raises(IgctlError, match="ig_user_id"):
        _ = g.ig_user_id


@respx.mock
def test_comments_list_reply_hide_delete():
    g, _ = make()
    respx.get(f"{BASE}/m1/comments").respond(
        json={
            "data": [
                {
                    "id": "c1",
                    "text": "nice",
                    "username": "bob",
                    "from": {"id": "9", "username": "bob"},
                    "replies": {"data": [{"id": "c2", "text": "ty", "username": "mybrand"}]},
                }
            ]
        }
    )
    reply = respx.post(f"{BASE}/c1/replies").respond(json={"id": "c3"})
    hide = respx.post(f"{BASE}/c1").respond(json={"success": True})
    delete = respx.delete(f"{BASE}/c2").respond(json={"success": True})

    comments = g.list_comments("m1")
    assert [(c.id, c.replied_to_id) for c in comments] == [("c1", None), ("c2", "c1")]
    assert comments[0].user_id == "9"

    made = g.comment("m1", "thanks", reply_to="c1")
    assert made.id == "c3" and made.username == "mybrand" and made.replied_to_id == "c1"
    assert form(reply.calls.last.request) == {"message": "thanks"}

    g.hide_comment("c1")
    assert form(hide.calls.last.request) == {"hide": "true"}
    g.delete_comment("m1", "c2")
    assert delete.called


@respx.mock
def test_media_insights_defaults_by_type():
    g, _ = make()
    respx.get(f"{BASE}/m1").respond(
        json={"id": "m1", "media_type": "VIDEO", "media_product_type": "REELS"}
    )
    route = respx.get(f"{BASE}/m1/insights").respond(
        json={"data": [{"name": "reach", "period": "lifetime", "values": [{"value": 42}]}]}
    )
    out = g.media_insights("m1")
    assert out[0].name == "reach" and out[0].value == 42
    assert "ig_reels_avg_watch_time" in route.calls.last.request.url.params["metric"]


@respx.mock
def test_account_insights_total_and_series():
    g, _ = make()
    route = respx.get(f"{BASE}/{IG}/insights").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "data": [
                    {"name": "reach", "period": "day", "total_value": {"value": 10}}
                    if req.url.params.get("metric_type") == "total_value"
                    else {
                        "name": "follower_count",
                        "period": "day",
                        "values": [
                            {"value": 3, "end_time": "2025-05-01T07:00:00+0000"},
                            {"value": 4, "end_time": "2025-05-02T07:00:00+0000"},
                        ],
                    }
                ]
            },
        )
    )
    out = g.account_insights(["reach", "follower_count"], days=45)
    # 45 days -> two windows for totals, series only for the last 30-day window
    assert [i.name for i in out].count("reach") == 2
    assert [i.value for i in out if i.name == "follower_count"] == [3, 4]
    assert route.call_count == 3


@respx.mock
def test_hashtag_media():
    g, _ = make()
    respx.get(f"{BASE}/ig_hashtag_search").respond(json={"data": [{"id": "h1"}]})
    route = respx.get(f"{BASE}/h1/recent_media").respond(
        json={"data": [{"id": "x", "media_type": "IMAGE", "caption": "#Coffee"}]}
    )
    media = g.hashtag_media("#coffee", limit=10, recent=True)
    assert media[0].hashtags == ["coffee"]
    assert route.calls.last.request.url.params["user_id"] == IG


def test_delete_media_requires_facebook_host():
    g, _ = make(graph_host="graph.instagram.com")
    with pytest.raises(NotSupported):
        g.delete_media("m1")
    with pytest.raises(NotSupported):
        g.edit_caption("m1", "x")


# --- media_host ------------------------------------------------------------------


# --- regressions -----------------------------------------------------------------


def _echo_host(tmp_path: Path, template: str) -> tuple[Config, Path]:
    """A cmd: media host that records each uploaded file and prints ``template``."""
    log = tmp_path / "uploads.txt"
    script = tmp_path / "up.py"
    script.write_text(
        "import sys, pathlib\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "log.open('a').write(str(p) + '\\n')\n"
        f"print({template!r}.format(name=p.name, stem=p.stem))\n"
    )
    return Config(media_host=f'cmd:"{sys.executable}" "{script}" {{path}}'), log


def test_non_jpeg_url_is_rejected():
    g, _ = make()
    with pytest.raises(IgctlError, match="only accepts JPEG"):
        g.publish(PublishRequest(media_type=MediaType.IMAGE, urls=["https://x/a.png"]))


@respx.mock
def test_local_png_is_converted_to_jpeg(tmp_path: Path):
    from PIL import Image

    png = tmp_path / "pic.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(png)
    cfg, log = _echo_host(tmp_path, "https://h/{name}")
    g = GraphBackend(
        Account(name="brand", ig_user_id=IG), httpx.Client(), cfg, sleep=lambda s: None
    )
    create = respx.post(f"{BASE}/{IG}/media").respond(json={"id": "c1"})
    respx.get(f"{BASE}/c1").respond(json={"status_code": "FINISHED"})
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m1"})
    respx.get(f"{BASE}/m1").respond(json={})

    g.publish(PublishRequest(media_type=MediaType.IMAGE, paths=[png]))

    uploaded = Path(log.read_text().splitlines()[0])
    assert uploaded.suffix == ".jpg"
    assert form(create.calls.last.request)["image_url"].endswith(".jpg")


@respx.mock
def test_carousel_video_detected_from_local_path(tmp_path: Path):
    from PIL import Image

    pic, clip = tmp_path / "a.jpg", tmp_path / "b.mp4"
    Image.new("RGB", (100, 100)).save(pic)
    clip.write_bytes(b"x")
    cfg, _ = _echo_host(tmp_path, "https://h/{stem}")  # extension-less hosted URLs
    g = GraphBackend(
        Account(name="brand", ig_user_id=IG), httpx.Client(), cfg, sleep=lambda s: None
    )
    ids = iter(["k1", "k2", "parent"])
    create = respx.post(f"{BASE}/{IG}/media").mock(
        side_effect=lambda req: httpx.Response(200, json={"id": next(ids)})
    )
    for cid in ("k1", "k2", "parent"):
        respx.get(f"{BASE}/{cid}").respond(json={"status_code": "FINISHED"})
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m1"})
    respx.get(f"{BASE}/m1").respond(json={})

    g.publish(PublishRequest(media_type=MediaType.CAROUSEL, paths=[pic, clip]))

    bodies = [form(c.request) for c in create.calls]
    assert bodies[0]["image_url"] == "https://h/a"
    assert bodies[1]["video_url"] == "https://h/b" and bodies[1]["media_type"] == "VIDEO"


@respx.mock
def test_carousel_extensionless_url_is_probed():
    g, _ = make()
    respx.head("https://cdn/v1").respond(headers={"content-type": "video/mp4"})
    respx.head("https://cdn/i1").respond(headers={"content-type": "image/jpeg"})
    ids = iter(["k1", "k2", "parent"])
    create = respx.post(f"{BASE}/{IG}/media").mock(
        side_effect=lambda req: httpx.Response(200, json={"id": next(ids)})
    )
    for cid in ("k1", "k2", "parent"):
        respx.get(f"{BASE}/{cid}").respond(json={"status_code": "FINISHED"})
    respx.post(f"{BASE}/{IG}/media_publish").respond(json={"id": "m1"})
    respx.get(f"{BASE}/m1").respond(json={})

    g.publish(
        PublishRequest(media_type=MediaType.CAROUSEL, urls=["https://cdn/i1", "https://cdn/v1"])
    )

    bodies = [form(c.request) for c in create.calls]
    assert "image_url" in bodies[0] and bodies[1]["video_url"] == "https://cdn/v1"


@respx.mock
def test_account_insights_periods():
    g, _ = make()
    route = respx.get(f"{BASE}/{IG}/insights").respond(json={"data": []})
    g.account_insights(["reach", "follower_count", "online_followers"], period="week", days=7)
    calls = {c.request.url.params["metric"]: c.request.url.params for c in route.calls}
    assert calls["online_followers"]["period"] == "lifetime"
    assert calls["follower_count"]["period"] == "day"
    assert calls["reach"]["period"] == "day"  # total_value metrics only take 'day'


@respx.mock
def test_media_insights_instagram_host_falls_back_for_stories():
    host = "https://graph.instagram.com/v23.0"
    g, _ = make(graph_host="graph.instagram.com")
    respx.get(f"{host}/s1").respond(json={"id": "s1", "media_type": "IMAGE"})
    route = respx.get(f"{host}/s1/insights").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"code": 100, "message": "likes not supported"}}),
            httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 5}]}]}),
        ]
    )
    out = g.media_insights("s1")
    assert out[0].value == 5
    assert route.calls[0].request.url.params["metric"].startswith("reach,views,likes")
    assert route.calls[1].request.url.params["metric"] == "reach,views,shares,total_interactions"


def test_discovery_and_hashtags_need_facebook_host():
    g, _ = make(graph_host="graph.instagram.com")
    with pytest.raises(NotSupported, match="Facebook Login"):
        g.hashtag_media("coffee")
    with pytest.raises(NotSupported, match="Facebook Login"):
        g.get_profile("nike")
    with pytest.raises(NotSupported, match="Facebook Login"):
        g.list_media("nike")
    assert not g.supports("hashtag_media") and not g.supports("delete_media")
    assert make()[0].supports("hashtag_media")


@respx.mock
def test_resolve_ig_user_id_pages_and_ambiguity():
    page2 = f"{BASE}/me/accounts?after=X"

    def accounts(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("after"):
            return httpx.Response(
                200,
                json={"data": [{"instagram_business_account": {"id": "22", "username": "two"}}]},
            )
        return httpx.Response(
            200,
            json={
                "data": [{"instagram_business_account": {"id": "11", "username": "one"}}],
                "paging": {"next": page2},
            },
        )

    respx.get(f"{BASE}/me/accounts").mock(side_effect=accounts)
    g = GraphBackend(Account(name="brand", username="two"), httpx.Client(), Config())
    assert g.ig_user_id == "22"  # found on the second page
    g = GraphBackend(Account(name="brand"), httpx.Client(), Config())
    with pytest.raises(IgctlError, match="several Instagram accounts"):
        _ = g.ig_user_id


@respx.mock
def test_network_errors_are_transient():
    from igctl.backends.base import TransientError

    g, _ = make()
    respx.get(f"{BASE}/{IG}").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TransientError):
        g.me()


def test_resolve_urls_keeps_urls():
    req = PublishRequest(media_type=MediaType.IMAGE, urls=["https://x/a.jpg"])
    assert resolve_urls(req, Config()) is req


def test_resolve_urls_cmd(tmp_path: Path):
    img = tmp_path / "my pic.jpg"
    img.write_bytes(b"x")
    script = tmp_path / "up.py"
    script.write_text(
        "import sys, pathlib\nprint('log')\nprint('https://h/' + pathlib.Path(sys.argv[1]).name)\n"
    )
    cfg = Config(media_host=f'cmd:"{sys.executable}" "{script}" {{path}}')
    out = resolve_urls(PublishRequest(media_type=MediaType.IMAGE, paths=[img]), cfg)
    assert out.urls == ["https://h/my pic.jpg"]


def test_resolve_urls_errors(tmp_path: Path, monkeypatch):
    img = tmp_path / "a.mp4"
    img.write_bytes(b"x")
    req = PublishRequest(media_type=MediaType.REEL, paths=[img])
    with pytest.raises(IgctlError, match="s3://"):
        resolve_urls(req, Config())
    with pytest.raises(IgctlError, match="images"):
        resolve_urls(req, Config(media_host="imgbb:KEY"))
    with pytest.raises(IgctlError, match="Unrecognised"):
        resolve_urls(req, Config(media_host="ftp://x"))
    with pytest.raises(IgctlError, match="URL"):
        resolve_urls(req, Config(media_host="cmd:echo nope {path}"))


@respx.mock
def test_resolve_urls_imgbb(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IMG_KEY", "k")
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    respx.post("https://api.imgbb.com/1/upload").respond(
        json={"data": {"url": "https://i.ibb.co/a.png"}}
    )
    out = resolve_urls(
        PublishRequest(media_type=MediaType.IMAGE, paths=[img]), Config(media_host="imgbb:IMG_KEY")
    )
    assert out.urls == ["https://i.ibb.co/a.png"]


def test_is_video():
    assert is_video("https://x/v.MP4?sig=1") and not is_video(Path("a.jpg"))


def test_upload_s3_presigns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import types

    from igctl.core.media_host import upload_file

    calls: list[tuple] = []

    class FakeS3:
        def upload_file(self, path, bucket, key, ExtraArgs):  # noqa: N803 - boto3 signature
            calls.append((bucket, key, ExtraArgs["ContentType"]))

        def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
            return f"https://{Params['Bucket']}.s3/{Params['Key']}?exp={ExpiresIn}"

    fake = types.SimpleNamespace(client=lambda name: FakeS3())
    monkeypatch.setitem(sys.modules, "boto3", fake)
    img = tmp_path / "a.JPG"
    img.write_bytes(b"x")
    url = upload_file(img, Config(media_host="s3://bucket/ig/"))
    bucket, key, ctype = calls[0]
    assert bucket == "bucket" and key.startswith("ig/") and key.endswith(".jpg")
    assert ctype == "image/jpeg"
    assert url == f"https://bucket.s3/{key}?exp=3600"
