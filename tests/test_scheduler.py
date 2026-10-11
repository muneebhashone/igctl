from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from igctl.backends.base import AuthError, Backend, RateLimited
from igctl.core.config import Account, Config, Limits
from igctl.core.models import Media, MediaType, Profile, PublishRequest, PublishResult
from igctl.core.ratelimit import Guard
from igctl.core.storage import Store, iso
from igctl.scheduler import queue
from igctl.scheduler.queue import CalendarError, ScheduleError, build_request
from igctl.scheduler.runner import StopFlag, backoff_delay, is_transient, run_due, run_loop
from igctl.scheduler.timeparse import TimeParseError, format_local, parse_when, resolve_tz

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # a Sunday


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("IGCTL_HOME", str(h))
    monkeypatch.delenv("IGCTL_ACCOUNT", raising=False)
    Config(
        default_account="brand",
        accounts={"brand": Account(name="brand"), "alt": Account(name="alt")},
    ).save()
    return h


@pytest.fixture
def store(home: Path) -> Store:
    s = Store()
    yield s
    s.close()


@pytest.fixture
def media(tmp_path: Path) -> dict[str, Path]:
    d = tmp_path / "media"
    d.mkdir()
    files = {n: d / n for n in ("a.jpg", "b.png", "clip.mp4", "notes.txt")}
    for p in files.values():
        if p.suffix in (".jpg", ".png"):
            Image.new("RGB", (100, 100)).save(p)  # real images: aspect ratio is validated
        else:
            p.write_bytes(b"x")
    return files


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, errors: list[Exception] | None = None):
        self.errors = list(errors or [])
        self.published: list[PublishRequest] = []

    def me(self) -> Profile:
        return Profile(id="1", username="me")

    def publish(self, req: PublishRequest) -> PublishResult:
        if self.errors:
            raise self.errors.pop(0)
        self.published.append(req)
        n = len(self.published)
        return PublishResult(media_id=f"m{n}", permalink=f"https://ig/p/{n}", backend=self.name)

    def get_profile(self, username: str) -> Profile:
        return Profile(id="2", username=username)

    def list_media(self, username: str | None = None, limit: int = 50) -> list[Media]:
        return []


class FakeContext:
    def __init__(self, account: str, store: Store, backend: FakeBackend, limits: Limits):
        self._backend = backend
        self.guard = Guard(account, limits, store, sleep=lambda s: None)

    def backend(self, prefer: str | None = None) -> Backend:
        return self._backend


def factory(store: Store, backend: FakeBackend, daily: int = 25):
    limits = Limits(min_delay=0, max_delay=0, daily={"publish": daily})
    made: list[str] = []

    def make(account: str) -> FakeContext:
        made.append(account)
        return FakeContext(account, store, backend, limits)

    make.made = made  # type: ignore[attr-defined]
    return make


def _enqueue(store: Store, media: dict[str, Path], run_at: datetime = NOW, account="brand") -> int:
    req = build_request([media["a.jpg"]], caption="hi")
    return queue.enqueue(store, account, req, run_at)


# --- time parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("now", NOW),
        ("+30m", NOW + timedelta(minutes=30)),
        ("+2h", NOW + timedelta(hours=2)),
        ("+1d", NOW + timedelta(days=1)),
        ("+1h30m", NOW + timedelta(hours=1, minutes=30)),
        ("in 2h", NOW + timedelta(hours=2)),
        ("tomorrow 18:00", datetime(2026, 10, 12, 18, 0, tzinfo=UTC)),
        ("today 6pm", datetime(2026, 10, 11, 18, 0, tzinfo=UTC)),
        ("fri 9am", datetime(2026, 10, 16, 9, 0, tzinfo=UTC)),
        ("sunday 09:00", datetime(2026, 10, 18, 9, 0, tzinfo=UTC)),  # today's 09:00 passed
        ("13:00", datetime(2026, 10, 11, 13, 0, tzinfo=UTC)),
        ("11:00", datetime(2026, 10, 12, 11, 0, tzinfo=UTC)),
        ("2026-12-01T08:30", datetime(2026, 12, 1, 8, 30, tzinfo=UTC)),
        ("2026-12-01 08:30+02:00", datetime(2026, 12, 1, 6, 30, tzinfo=UTC)),
        ("2026-12-01T08:30Z", datetime(2026, 12, 1, 8, 30, tzinfo=UTC)),
    ],
)
def test_parse_when_utc(text: str, expected: datetime) -> None:
    assert parse_when(text, "UTC", now=NOW) == expected


def test_parse_when_offsets_and_zones() -> None:
    assert parse_when("tomorrow 18:00", "+05:00", now=NOW) == datetime(
        2026, 10, 12, 13, 0, tzinfo=UTC
    )
    # Europe/London is on BST (UTC+1) in October, GMT in December.
    assert parse_when("2026-10-20 10:00", "Europe/London") == datetime(
        2026, 10, 20, 9, 0, tzinfo=UTC
    )
    assert parse_when("2026-12-20 10:00", "Europe/London") == datetime(
        2026, 12, 20, 10, 0, tzinfo=UTC
    )


def test_parse_when_local_default_is_aware_utc() -> None:
    dt = parse_when("2026-12-01T08:30")
    assert dt.tzinfo is UTC
    assert dt == datetime(2026, 12, 1, 8, 30).astimezone().astimezone(UTC)


@pytest.mark.parametrize("bad", ["", "soon", "tomorrow", "25:00", "+5x", "today 13pm"])
def test_parse_when_rejects(bad: str) -> None:
    with pytest.raises(TimeParseError):
        parse_when(bad, "UTC", now=NOW)


def test_resolve_tz() -> None:
    assert resolve_tz(None) is None
    assert resolve_tz("UTC") is UTC
    assert resolve_tz("-03:30") == timezone(-timedelta(hours=3, minutes=30))
    with pytest.raises(TimeParseError):
        resolve_tz("Mars/Olympus")
    assert format_local(iso(NOW), "UTC") == "2026-10-11 12:00 UTC"
    assert format_local(NOW, "+05:00") == "2026-10-11 17:00 UTC+05:00"


# --- request building -------------------------------------------------------


def test_build_request_infers_types_and_absolutizes(media, monkeypatch) -> None:
    monkeypatch.chdir(media["a.jpg"].parent)
    req = build_request(["a.jpg"], caption="x")
    assert req.media_type is MediaType.IMAGE
    assert req.paths == [media["a.jpg"].resolve()] and req.paths[0].is_absolute()
    assert build_request(["clip.mp4"]).media_type is MediaType.REEL
    assert build_request(["a.jpg", "clip.mp4"]).media_type is MediaType.CAROUSEL
    url_req = build_request(["https://cdn.example.com/x.jpg"], "image")
    assert url_req.urls == ["https://cdn.example.com/x.jpg"] and not url_req.paths


@pytest.mark.parametrize(
    ("files", "mtype", "caption", "msg"),
    [
        ([], None, "", "At least one"),
        (["missing.jpg"], None, "", "File not found"),
        (["notes.txt"], None, "", "Unsupported"),
        (["a.jpg"], "carousel", "", "carousel needs"),
        (["a.jpg", "b.png"], "image", "", "exactly one"),
        (["clip.mp4"], "image", "", "needs an image"),
        (["a.jpg"], "reel", "", "needs a video"),
        (["a.jpg"], "gif", "", "Unknown media type"),
        (["a.jpg"], None, "x" * 2201, "2200"),
        (["a.jpg"], None, " ".join(f"#t{i}" for i in range(31)), "hashtags"),
        (["a.jpg", "https://x/y.jpg"], "carousel", "", "not both"),
    ],
)
def test_build_request_validation(media, monkeypatch, files, mtype, caption, msg) -> None:
    monkeypatch.chdir(media["a.jpg"].parent)
    with pytest.raises(ScheduleError, match=msg):
        build_request(files, mtype, caption)


# --- queue state transitions ------------------------------------------------


def test_enqueue_cancel_retry(store, media) -> None:
    pid = _enqueue(store, media)
    row = queue.get(store, pid)
    assert row["status"] == "pending" and row["run_at"] == iso(NOW)
    assert PublishRequest.model_validate_json(row["request"]).caption == "hi"

    queue.cancel(store, pid)
    assert queue.get(store, pid)["status"] == "cancelled"
    with pytest.raises(ScheduleError, match="can't be cancelled"):
        queue.cancel(store, pid)

    later = NOW + timedelta(hours=3)
    queue.retry(store, pid, later)
    row = queue.get(store, pid)
    assert (row["status"], row["attempts"], row["run_at"]) == ("pending", 0, iso(later))
    with pytest.raises(ScheduleError, match="only failed or cancelled"):
        queue.retry(store, pid)
    with pytest.raises(ScheduleError, match="No scheduled post"):
        queue.get(store, 999)
    with pytest.raises(ScheduleError, match="timezone-aware"):
        queue.enqueue(store, "brand", build_request([media["a.jpg"]]), datetime(2026, 1, 1))


def test_recover_stale(store, media) -> None:
    old = _enqueue(store, media, NOW - timedelta(hours=5))
    fresh = _enqueue(store, media, NOW - timedelta(hours=5))
    exhausted = _enqueue(store, media, NOW - timedelta(hours=5))
    for pid in (old, fresh, exhausted):
        assert store.claim_post(pid)
    queue.record_claim(store, old, NOW - timedelta(hours=2))
    queue.record_claim(store, fresh, NOW - timedelta(minutes=10))
    store._conn.execute("UPDATE scheduled_posts SET attempts=3 WHERE id=?", (exhausted,))
    store._conn.commit()  # exhausted has no claim row: falls back to run_at

    assert sorted(queue.recover_stale(store, now=NOW)) == sorted([old, exhausted])
    assert queue.get(store, old)["status"] == "pending"
    assert "stale" in queue.get(store, old)["error"]
    assert queue.get(store, fresh)["status"] == "running"
    assert queue.get(store, exhausted)["status"] == "failed"


# --- calendar import --------------------------------------------------------


def test_import_csv(store, media) -> None:
    cal = media["a.jpg"].parent / "cal.csv"
    cal.write_text(
        "when,type,files,caption,account\n"
        "2026-11-01T10:00,,a.jpg,First post,\n"
        '2026-11-02T10:00,carousel,a.jpg;b.png,"Two, items",alt\n'
        ",,,,\n",
        encoding="utf-8",
    )
    results = queue.import_calendar(
        store, cal, default_account="brand", tz="UTC", known_accounts=["brand", "alt"]
    )
    assert [e.account for _, e in results] == ["brand", "alt"]
    rows = queue.list_posts(store)
    assert len(rows) == 2
    second = PublishRequest.model_validate_json(rows[1]["request"])
    assert second.media_type is MediaType.CAROUSEL and second.caption == "Two, items"
    assert second.paths == [media["a.jpg"].resolve(), media["b.png"].resolve()]
    assert rows[0]["run_at"] == iso(datetime(2026, 11, 1, 10, tzinfo=UTC))


def test_import_yaml_with_lists_and_row_tz(store, media) -> None:
    cal = media["a.jpg"].parent / "cal.yaml"
    cal.write_text(
        yaml.safe_dump(
            {
                "posts": [
                    {"when": "2026-11-01 10:00", "files": ["clip.mp4"], "caption": "reel"},
                    {"when": "2026-11-01 10:00", "files": "a.jpg", "tz": "+02:00"},
                ]
            }
        ),
        encoding="utf-8",
    )
    results = queue.import_calendar(store, cal, default_account="brand", tz="UTC")
    assert results[0][1].request.media_type is MediaType.REEL
    assert results[1][1].run_at == datetime(2026, 11, 1, 8, tzinfo=UTC)


def test_import_validates_all_rows_before_inserting(store, media) -> None:
    cal = media["a.jpg"].parent / "bad.csv"
    cal.write_text(
        "when,type,files,caption,account\n"
        "2026-11-01T10:00,,a.jpg,ok,\n"
        "whenever,,a.jpg,bad time,\n"
        "2026-11-01T10:00,,missing.jpg,,\n"
        "2026-11-01T10:00,,a.jpg,,ghost\n"
        "2026-11-01T10:00,,,,\n",
        encoding="utf-8",
    )
    with pytest.raises(CalendarError) as exc:
        queue.import_calendar(
            store, cal, default_account="brand", tz="UTC", known_accounts=["brand"]
        )
    errs = exc.value.errors
    assert len(errs) == 4
    assert errs[0].startswith("row 2") and "Can't understand" in errs[0]
    assert "File not found" in errs[1] and "unknown account 'ghost'" in errs[2]
    assert "media file is required" in errs[3]
    assert queue.list_posts(store) == []


def test_import_rejects_unknown_format(store, tmp_path) -> None:
    f = tmp_path / "cal.json"
    f.write_text("[]")
    with pytest.raises(ScheduleError, match="Unsupported calendar"):
        queue.import_calendar(store, f, default_account="brand")


# --- runner -----------------------------------------------------------------


def test_run_due_publishes_due_posts_only(store, media) -> None:
    due = _enqueue(store, media, NOW - timedelta(minutes=1))
    future = _enqueue(store, media, NOW + timedelta(hours=1))
    backend = FakeBackend()
    make = factory(store, backend)

    outcomes = run_due(store, make, now=NOW)
    assert [(o.post_id, o.status, o.media_id) for o in outcomes] == [(due, "done", "m1")]
    row = queue.get(store, due)
    assert row["status"] == "done" and json.loads(row["result"])["media_id"] == "m1"
    assert queue.get(store, future)["status"] == "pending"
    assert len(backend.published) == 1
    assert store.count_actions_since("brand", "publish", NOW - timedelta(days=1)) == 1


def test_run_due_retries_transient_then_fails(store, media) -> None:
    pid = _enqueue(store, media, NOW)
    backend = FakeBackend([RateLimited("slow down")] * 3)
    make = factory(store, backend)

    o1 = run_due(store, make, now=NOW)[0]
    assert o1.status == "retry" and o1.next_run_at == NOW + timedelta(minutes=5)
    row = queue.get(store, pid)
    assert (row["status"], row["attempts"]) == ("pending", 1) and "slow down" in row["error"]
    assert run_due(store, make, now=NOW) == []  # not due yet

    t2 = NOW + timedelta(minutes=5)
    assert run_due(store, make, now=t2)[0].next_run_at == t2 + timedelta(minutes=10)
    o3 = run_due(store, make, now=t2 + timedelta(minutes=10))[0]
    assert o3.status == "failed"
    row = queue.get(store, pid)
    assert (row["status"], row["attempts"]) == ("failed", 3)


def test_run_due_recovers_after_transient(store, media) -> None:
    pid = _enqueue(store, media, NOW)
    make = factory(store, FakeBackend([ConnectionError("reset")]))
    assert run_due(store, make, now=NOW)[0].status == "retry"
    assert run_due(store, make, now=NOW + timedelta(hours=1))[0].status == "done"
    assert queue.get(store, pid)["attempts"] == 2


def test_run_due_permanent_error_fails_immediately(store, media) -> None:
    pid = _enqueue(store, media, NOW)
    make = factory(store, FakeBackend([AuthError("session expired")]))
    assert run_due(store, make, now=NOW)[0].status == "failed"
    assert "AuthError: session expired" in queue.get(store, pid)["error"]


def test_run_due_missing_file_fails(store, media) -> None:
    pid = _enqueue(store, media, NOW)
    media["a.jpg"].unlink()
    assert run_due(store, factory(store, FakeBackend()), now=NOW)[0].status == "failed"
    assert "no longer exist" in queue.get(store, pid)["error"]


def test_run_due_daily_cap_defers_without_burning_attempts(store, media) -> None:
    pid = _enqueue(store, media, NOW)
    outcome = run_due(store, factory(store, FakeBackend(), daily=0), now=NOW)[0]
    assert outcome.status == "retry" and outcome.next_run_at == NOW + timedelta(hours=1)
    row = queue.get(store, pid)
    assert (row["status"], row["attempts"]) == ("pending", 0)


def test_run_due_dry_run_touches_nothing(store, media) -> None:
    pid = _enqueue(store, media, NOW)
    backend = FakeBackend()
    assert run_due(store, factory(store, backend), now=NOW, dry_run=True)[0].status == "dry-run"
    assert queue.get(store, pid)["status"] == "pending" and not backend.published


def test_run_due_skips_posts_claimed_elsewhere(store, media, monkeypatch) -> None:
    pid = _enqueue(store, media, NOW)
    monkeypatch.setattr(store, "claim_post", lambda post_id: False)
    assert run_due(store, factory(store, FakeBackend()), now=NOW)[0].status == "skipped"
    assert queue.get(store, pid)["status"] == "pending"


def test_run_due_caches_context_per_account(store, media) -> None:
    for acct in ("brand", "brand", "alt"):
        _enqueue(store, media, NOW, account=acct)
    make = factory(store, FakeBackend())
    assert [o.status for o in run_due(store, make, now=NOW)] == ["done"] * 3
    assert sorted(make.made) == ["alt", "brand"]


def test_is_transient_and_backoff() -> None:
    class PleaseWaitFewMinutes(Exception):  # mimics instagrapi's class name
        pass

    assert is_transient(RateLimited("x")) and is_transient(PleaseWaitFewMinutes())
    assert not is_transient(AuthError("x")) and not is_transient(ValueError("x"))
    assert [backoff_delay(n).total_seconds() / 60 for n in (1, 2, 3)] == [5, 10, 20]


def test_run_loop_stops_on_flag(store, media) -> None:
    _enqueue(store, media, datetime.now(UTC) - timedelta(minutes=1))
    stop = StopFlag()
    seen = []

    def on_outcome(o):
        seen.append(o.status)
        stop.set()

    run_loop(store, factory(store, FakeBackend()), interval=0.01, stop=stop, on_outcome=on_outcome)
    assert seen == ["done"]


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def cli(home, monkeypatch):
    from igctl.cli import schedule as schedule_cli

    runner = CliRunner()

    def invoke(*args: str):
        return runner.invoke(schedule_cli.app, list(args), catch_exceptions=False)

    invoke.module = schedule_cli  # type: ignore[attr-defined]
    return invoke


def test_cli_add_list_show_cancel_retry(cli, media) -> None:
    res = cli("add", str(media["a.jpg"]), str(media["b.png"]), "--at", "+1h", "-c", "hello")
    assert res.exit_code == 0, res.output
    with _store() as s:
        rows = queue.list_posts(s)
    assert len(rows) == 1 and rows[0]["account"] == "brand"
    req = PublishRequest.model_validate_json(rows[0]["request"])
    assert req.media_type is MediaType.CAROUSEL and req.caption == "hello"

    pid = str(rows[0]["id"])
    assert "hello" in cli("list").output
    assert "No scheduled posts" in cli("list", "--status", "done").output
    assert "carousel" in cli("show", pid).output
    assert cli("cancel", pid).exit_code == 0
    with _store() as s:
        assert queue.get(s, int(pid))["status"] == "cancelled"
    assert cli("retry", pid, "--at", "now").exit_code == 0
    with _store() as s:
        assert queue.get(s, int(pid))["status"] == "pending"


def test_cli_add_caption_file_account_and_errors(cli, media, tmp_path) -> None:
    cap = tmp_path / "cap.txt"
    cap.write_text("from file\n", encoding="utf-8")
    res = cli(
        "add", str(media["clip.mp4"]), "--at", "2026-12-01 10:00", "--tz", "UTC",
        "--caption-file", str(cap), "--account", "alt", "--no-feed",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    with _store() as s:
        row = queue.list_posts(s)[0]
    req = PublishRequest.model_validate_json(row["request"])
    assert row["account"] == "alt" and row["run_at"] == "2026-12-01T10:00:00+00:00"
    assert req.caption == "from file" and req.media_type is MediaType.REEL
    assert req.share_to_feed is False

    with pytest.raises(ScheduleError, match="File not found"):
        cli("add", "nope.jpg", "--at", "now")


def test_cli_import(cli, media) -> None:
    cal = media["a.jpg"].parent / "cal.csv"
    cal.write_text("when,files,caption\n+1d,a.jpg,one\n+2d,b.png,two\n", encoding="utf-8")
    res = cli("import", str(cal))
    assert res.exit_code == 0, res.output
    with _store() as s:
        assert len(queue.list_posts(s, "pending")) == 2


def test_cli_run_once(cli, media, monkeypatch) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(cli.module, "_context_factory", lambda root: factory(root.store, backend))
    cli("add", str(media["a.jpg"]), "--at", "now")
    res = cli("run", "--once")
    assert res.exit_code == 0, res.output
    assert len(backend.published) == 1
    assert "Nothing due" in cli("run").output


def test_cli_run_help_has_install_guidance(cli) -> None:
    out = cli("run", "--help").output
    assert "schtasks" in out and "crontab" in out and "systemd" in out


class _store:
    """Context manager for a short-lived Store in CLI tests."""

    def __enter__(self) -> Store:
        self.s = Store()
        return self.s

    def __exit__(self, *exc) -> None:
        self.s.close()
