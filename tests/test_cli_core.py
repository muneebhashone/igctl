"""Tests for igctl auth / post / download commands (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import typer
from PIL import Image
from typer.testing import CliRunner

from igctl.backends.base import AuthError, Backend, IgctlError, NotSupported
from igctl.cli import auth, download, post
from igctl.core import config as config_mod
from igctl.core.config import Account, Config, get_secret, set_secret
from igctl.core.context import Context
from igctl.core.models import Media, MediaType, Profile, PublishRequest, PublishResult
from igctl.core.storage import Store

runner = CliRunner()


class FakeBackend(Backend):
    def __init__(self, name: str = "private", fail_me: bool = False):
        self.name = name
        self.fail_me = fail_me
        self.published: list[PublishRequest] = []
        self.deleted: list[str] = []
        self.edited: list[tuple[str, str]] = []
        self.downloaded: list[tuple[str, str]] = []
        self.logins: list[tuple] = []

    def login(self, username, password, verification_code=None):
        self.logins.append((username, password, verification_code))

    def me(self) -> Profile:
        if self.fail_me:
            raise AuthError("bad token")
        return Profile(id="17841400000000000", username="brand")

    def publish(self, req: PublishRequest) -> PublishResult:
        self.published.append(req)
        return PublishResult(
            media_id="999", permalink="https://instagram.com/p/X/", backend=self.name
        )

    def delete_media(self, media_id: str) -> None:
        self.deleted.append(media_id)

    def edit_caption(self, media_id: str, caption: str) -> None:
        self.edited.append((media_id, caption))

    def get_profile(self, username: str) -> Profile:
        return Profile(id="1", username=username)

    def list_media(self, username=None, limit=50):
        return [
            Media(id=str(i), code=f"C{i}", media_type=MediaType.IMAGE, caption=f"post {i}")
            for i in range(min(limit, 3))
        ]

    def download_media(self, media_id: str, dest: str) -> list[str]:
        if media_id == "1":
            raise RuntimeError("boom")
        self.downloaded.append((media_id, dest))
        return [str(Path(dest) / f"{media_id}.jpg")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("IGCTL_ACCOUNT", raising=False)
    secrets: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(config_mod.keyring, "get_password", lambda s, k: secrets.get((s, k)))
    monkeypatch.setattr(
        config_mod.keyring, "set_password", lambda s, k, v: secrets.__setitem__((s, k), v)
    )
    monkeypatch.setattr(
        config_mod.keyring, "delete_password", lambda s, k: secrets.pop((s, k), None)
    )
    fake = FakeBackend()
    monkeypatch.setattr(Context, "backend", lambda self, prefer=None: fake)
    monkeypatch.chdir(tmp_path)
    return fake


@pytest.fixture
def account(env):
    cfg = Config(default_account="main", accounts={"main": Account(name="main", backend="private")})
    cfg.save()
    return env


def make_app(dry_run: bool = False, backend: str | None = None) -> typer.Typer:
    root = typer.Typer()

    @root.callback()
    def cb(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
        ctx.obj = Context(dry_run=dry_run, json_output=json_output, backend_override=backend)

    root.add_typer(auth.app, name="auth")
    root.add_typer(post.app, name="post")
    root.add_typer(download.app, name="download")
    return root


def invoke(*args: str, dry_run: bool = False, input: str | None = None, backend: str | None = None):
    return runner.invoke(make_app(dry_run, backend), list(args), input=input)


def img(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, "white").save(path)
    return path


# --- auth ------------------------------------------------------------------------


def test_login_graph_saves_account_and_token(env, monkeypatch):
    graph = FakeBackend("graph")
    monkeypatch.setattr(auth, "make_backend", lambda name, acct: graph)
    r = invoke("auth", "login", "brand", "--token", "TOK", "--host", "graph.instagram.com")
    assert r.exit_code == 0, r.output
    cfg = Config.load()
    assert cfg.default_account == "brand"
    acct = cfg.accounts["brand"]
    assert acct.graph_host == "graph.instagram.com"
    assert acct.ig_user_id == "17841400000000000"
    assert acct.username == "brand"
    assert get_secret("brand", "token") == "TOK"


def test_login_graph_failure_rolls_back_token(env, monkeypatch):
    set_secret("brand", "token", "OLD")
    monkeypatch.setattr(auth, "make_backend", lambda n, a: FakeBackend("graph", fail_me=True))
    r = invoke("auth", "login", "brand", "--token", "NEW")
    assert isinstance(r.exception, AuthError)
    assert get_secret("brand", "token") == "OLD"
    assert "brand" not in Config.load().accounts


def test_login_honours_global_backend(env, monkeypatch):
    be = FakeBackend("private")
    made: list[str] = []
    monkeypatch.setattr(auth, "make_backend", lambda n, a: made.append(n) or be)
    r = invoke("auth", "login", "p2", "-u", "brand", "--password", "pw", backend="private")
    assert r.exit_code == 0, r.output
    assert made == ["private"] and Config.load().accounts["p2"].backend == "private"
    r = invoke("auth", "login", "p3", "--backend", "graph", "--token", "T", backend="private")
    assert r.exit_code != 0  # conflicting global and local --backend


def test_login_private_prompts_password(env, monkeypatch):
    be = FakeBackend("private")
    monkeypatch.setattr(auth, "make_backend", lambda n, a: be)
    r = invoke(
        "auth", "login", "p1", "--backend", "private", "-u", "brand", "--2fa", "123456",
        input="s3cret\n",
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert be.logins == [("brand", "s3cret", "123456")]
    assert get_secret("p1", "password") == "s3cret"
    assert Config.load().accounts["p1"].backend == "private"


def test_second_login_keeps_default(account, monkeypatch):
    monkeypatch.setattr(auth, "make_backend", lambda n, a: FakeBackend("graph"))
    assert invoke("auth", "login", "other", "--token", "T").exit_code == 0
    assert Config.load().default_account == "main"


def test_list_use_logout(account):
    r = invoke("--json", "auth", "list")
    assert json.loads(r.stdout)[0]["name"] == "main"
    cfg = Config.load()
    cfg.accounts["b"] = Account(name="b")
    cfg.save()
    assert invoke("auth", "use", "b").exit_code == 0
    assert Config.load().default_account == "b"
    assert isinstance(invoke("auth", "use", "nope").exception, IgctlError)

    set_secret("b", "token", "T")
    session = config_mod.sessions_dir() / "b.json"
    session.write_text("{}")
    assert invoke("auth", "logout", "b").exit_code == 0
    assert get_secret("b", "token") is None and not session.exists()
    assert "b" in Config.load().accounts
    assert invoke("auth", "logout", "b", "--purge").exit_code == 0
    cfg = Config.load()
    assert "b" not in cfg.accounts and cfg.default_account == "main"


def test_whoami(account):
    r = invoke("--json", "auth", "whoami")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["username"] == "brand"


def test_set_limit(account):
    r = invoke("auth", "set-limit", "main", "like", "10", "--min-delay", "2", "--max-delay", "4")
    assert r.exit_code == 0, r.output
    limits = Config.load().accounts["main"].limits
    assert limits.daily["like"] == 10 and limits.min_delay == 2 and limits.max_delay == 4
    assert invoke("auth", "set-limit", "main", "--min-delay", "9").exit_code != 0


@respx.mock
def test_token_refresh_instagram(env):
    Config(accounts={"g": Account(name="g", graph_host="graph.instagram.com")}).save()
    set_secret("g", "token", "OLD")
    route = respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 5184000})
    )
    r = invoke("auth", "token-refresh", "g")
    assert r.exit_code == 0, r.output
    assert route.calls[0].request.url.params["grant_type"] == "ig_refresh_token"
    assert get_secret("g", "token") == "NEW"


@respx.mock
def test_token_refresh_facebook(env):
    Config(accounts={"f": Account(name="f")}).save()
    set_secret("f", "token", "OLD")
    r = invoke("auth", "token-refresh", "f")
    assert isinstance(r.exception, IgctlError) and "fb_exchange_token" in str(r.exception)

    route = respx.get("https://graph.facebook.com/v23.0/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "LL", "token_type": "bearer"})
    )
    r = invoke("auth", "token-refresh", "f", "--app-id", "1", "--app-secret", "s")
    assert r.exit_code == 0, r.output
    assert route.calls[0].request.url.params["fb_exchange_token"] == "OLD"
    assert get_secret("f", "token") == "LL"


@respx.mock
def test_token_refresh_error(env):
    Config(accounts={"g": Account(name="g", graph_host="graph.instagram.com")}).save()
    set_secret("g", "token", "OLD")
    respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(400, json={"error": {"message": "expired"}})
    )
    r = invoke("auth", "token-refresh", "g")
    assert isinstance(r.exception, AuthError) and "expired" in str(r.exception)
    assert get_secret("g", "token") == "OLD"


# --- post validation -------------------------------------------------------------


def test_validate_aspect_ratio(tmp_path):
    ok_img = img(tmp_path / "ok.jpg", (1080, 1350))
    tall = img(tmp_path / "tall.jpg", (1080, 1920))
    assert post.validate_request(PublishRequest(media_type=MediaType.IMAGE, paths=[ok_img])) == []
    with pytest.raises(IgctlError, match="aspect ratio"):
        post.validate_request(PublishRequest(media_type=MediaType.IMAGE, paths=[tall]))
    warnings = post.validate_request(PublishRequest(media_type=MediaType.STORY, paths=[ok_img]))
    assert warnings and "9:16" in warnings[0]
    assert post.validate_request(PublishRequest(media_type=MediaType.STORY, paths=[tall])) == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"caption": "x" * 2201}, "2200"),
        ({"caption": " ".join(f"#t{i}" for i in range(31))}, "hashtags"),
        ({"user_tags": [f"u{i}" for i in range(21)]}, "user tags"),
    ],
)
def test_validate_caption_limits(tmp_path, kwargs, match):
    p = img(tmp_path / "a.jpg", (1000, 1000))
    with pytest.raises(IgctlError, match=match):
        post.validate_request(PublishRequest(media_type=MediaType.IMAGE, paths=[p], **kwargs))


def test_validate_sources(tmp_path):
    with pytest.raises(IgctlError, match="not found"):
        post.validate_request(
            PublishRequest(media_type=MediaType.IMAGE, paths=[tmp_path / "x.jpg"])
        )
    vid = tmp_path / "v.gif"
    vid.write_bytes(b"x")
    with pytest.raises(IgctlError, match="not allowed"):
        post.validate_request(PublishRequest(media_type=MediaType.REEL, paths=[vid]))
    with pytest.raises(IgctlError, match="2-10"):
        post.validate_request(
            PublishRequest(media_type=MediaType.CAROUSEL, urls=["https://x/a.jpg"])
        )
    # extension-less CDN URLs pass; known-bad extensions don't
    post.validate_request(PublishRequest(media_type=MediaType.VIDEO, urls=["https://cdn/x?id=1"]))
    with pytest.raises(IgctlError, match="not allowed"):
        post.validate_request(PublishRequest(media_type=MediaType.VIDEO, urls=["https://x/a.jpg"]))


def test_post_at_matches_schedule_add(account, tmp_path):
    # `post --at` and `schedule add` share validation, past-time handling and JSON shape.
    p = img(tmp_path / "a.jpg", (1080, 1080))
    r = invoke("--json", "post", "photo", str(p), "--at", "2020-01-01T00:00Z", "-c", "x")
    assert r.exit_code == 0, r.output
    assert "in the past" in r.stderr  # warned and queued, like `schedule add`
    row = json.loads(r.stdout)
    assert row["status"] == "pending" and row["account"] == "main"
    assert row["run_at"] == "2020-01-01T00:00:00+00:00"
    assert row["request"]["caption"] == "x"
    r = invoke("post", "photo", str(p), "--at", "tomorrow")
    assert isinstance(r.exception, IgctlError)


# --- post commands ---------------------------------------------------------------


def test_post_photo_publishes(account, tmp_path):
    p = img(tmp_path / "a.jpg", (1080, 1080))
    r = invoke(
        "--json", "post", "photo", str(p), "-c", "hi #x", "--tag", "@bob", "--location", "Paris",
        "--yes",
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    req = account.published[0]
    assert req.media_type is MediaType.IMAGE and req.paths == [p.resolve()]
    assert req.user_tags == ["bob"] and req.location == "Paris" and req.caption == "hi #x"
    assert json.loads(r.stdout)["media_id"] == "999"
    assert Store().recent_actions("main")[0]["action"] == "publish"


def test_post_dry_run_does_not_publish(account, tmp_path):
    p = img(tmp_path / "a.jpg", (1080, 1080))
    r = invoke("post", "photo", str(p), dry_run=True)
    assert r.exit_code == 0, r.output
    assert account.published == [] and "dry-run" in r.output


def test_post_confirmation_abort(account, tmp_path):
    p = img(tmp_path / "a.jpg", (1080, 1080))
    r = invoke("post", "photo", str(p), input="n\n")
    assert r.exit_code != 0 and account.published == []


def test_post_reel_cover_and_caption_file(account, tmp_path):
    v = tmp_path / "r.mp4"
    v.write_bytes(b"\x00")
    cover = img(tmp_path / "c.jpg", (1080, 1920))
    cap = tmp_path / "cap.txt"
    cap.write_text("from file\n", encoding="utf-8")
    r = invoke(
        "post", "reel", str(v), "--cover", str(cover), "--no-feed", "--caption-file", str(cap),
        "-y",
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    req = account.published[0]
    assert req.media_type is MediaType.REEL and not req.share_to_feed
    assert req.cover == cover.resolve() and req.caption == "from file"


def test_post_carousel_urls(account):
    urls = ["--url", "https://x/a.jpg", "--url", "https://x/b.mp4"]
    r = invoke("post", "carousel", *urls, "-y")
    assert r.exit_code == 0, r.output
    assert account.published[0].urls == ["https://x/a.jpg", "https://x/b.mp4"]


def test_post_graph_schedule_local_file_needs_media_host(account, tmp_path):
    cfg = Config.load()
    cfg.accounts["main"].backend = "graph"
    cfg.save()
    p = img(tmp_path / "a.jpg", (1080, 1080))
    r = invoke("post", "photo", str(p), "--at", "+1h")
    assert isinstance(r.exception, IgctlError) and "media_host" in str(r.exception)
    assert Store().list_posts() == []


def test_post_graph_local_file_needs_url(account, tmp_path):
    account.name = "graph"
    p = img(tmp_path / "a.jpg", (1080, 1080))
    r = invoke("post", "photo", str(p), "-y")
    assert isinstance(r.exception, IgctlError) and "--url" in str(r.exception)


def test_post_schedule_enqueues(account, tmp_path):
    p = img(tmp_path / "a.jpg", (1080, 1080))
    when = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    r = invoke("post", "photo", str(p), "-c", "later", "--at", when.isoformat())
    assert r.exit_code == 0, r.output
    assert account.published == []
    (row,) = Store().list_posts("pending")
    assert row["account"] == "main"
    assert datetime.fromisoformat(row["run_at"]) == when
    req = PublishRequest.model_validate_json(row["request"])
    assert req.caption == "later" and req.paths == [p.resolve()]


def test_json_output_is_clean_for_write_commands(account):
    # The confirmation prompt goes to stderr, and write-only commands still print JSON.
    r = invoke("--json", "post", "delete", "123", input="y\n")
    assert r.exit_code == 0, r.output
    assert "Delete media" not in r.stdout
    # (CliRunner echoes the typed answer to stdout; a real terminal echoes it to the tty.)
    assert json.loads(r.stdout[r.stdout.index("{") :]) == {
        "ok": True,
        "media_id": "123",
        "action": "delete",
        "dry_run": False,
    }
    assert "Delete media" in r.stderr
    r = invoke("--json", "post", "edit", "123", "-c", "new")
    assert json.loads(r.stdout)["ok"] is True


def test_emit_never_parses_user_data_as_markup(capsys):
    from igctl.cli._common import emit

    ctx = Context()
    emit(ctx, [{"caption": "sale [/] ends", "user": "[bold]x[/bold]"}], title="Users: [/]")
    emit(ctx, {"caption": "[red]x"}, title="@[/]")
    emit(ctx, "plain [/] text")
    out = capsys.readouterr().out
    assert "sale [/] ends" in out and "[bold]x[/bold]" in out and "[red]x" in out


def test_post_delete_edit_list(account):
    url = "https://www.instagram.com/p/B1LbfVPlwIA/?igshid=x"
    assert invoke("post", "delete", url, "-y").exit_code == 0
    assert account.deleted == ["2110901750722920960"]
    assert invoke("post", "edit", "123", "-c", "new").exit_code == 0
    assert account.edited == [("123", "new")]
    r = invoke("--json", "post", "list", "--limit", "2")
    assert [m["id"] for m in json.loads(r.stdout)] == ["0", "1"]


def test_post_delete_dry_run(account):
    r = invoke("post", "delete", "123", dry_run=True)
    assert r.exit_code == 0 and account.deleted == []


# --- download --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("B1LbfVPlwIA", "2110901750722920960"),
        ("https://instagram.com/reel/B-fKL9qpeab/", "2278584739065882267"),
        ("instagram.com/someone/p/B1LbfVPlwIA", "2110901750722920960"),
        ("2110901750722920960_123", "2110901750722920960_123"),
    ],
)
def test_media_ref_to_id(ref, expected):
    assert download.media_ref_to_id(ref) == expected


def test_media_ref_bad_url():
    with pytest.raises(IgctlError):
        download.media_ref_to_id("https://instagram.com/someone/")


def test_download_post_and_profile(account, tmp_path):
    r = invoke("download", "post", "B1LbfVPlwIA", "--dest", str(tmp_path / "out"))
    assert r.exit_code == 0, r.output
    assert account.downloaded[0][0] == "2110901750722920960"
    assert (tmp_path / "out").is_dir()

    account.downloaded.clear()
    r = invoke("--json", "download", "profile", "@nasa", "--limit", "3")
    assert r.exit_code == 1, r.output  # a failed post must be visible to cron wrappers
    data = json.loads(r.stdout)
    assert data["failed"] == 1 and len(data["files"]) == 2
    assert Path(data["dest"]) == Path("nasa")


def test_download_stories(account, tmp_path):
    r = invoke("download", "stories", "nasa")
    assert isinstance(r.exception, NotSupported)
    account.download_stories = lambda username, dest: [f"{dest}/s1.mp4"]
    r = invoke("--json", "download", "stories", "nasa", "-d", str(tmp_path))
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == [{"file": f"{tmp_path}/s1.mp4"}]
