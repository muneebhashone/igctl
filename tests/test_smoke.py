"""Whole-app smoke tests: every command renders --help, and both real backends honour the
contract the CLI relies on (constructed offline with fake credentials)."""

from __future__ import annotations

import inspect

import pytest
import typer.main
from typer.testing import CliRunner

from igctl.backends.base import Backend, NotSupported
from igctl.backends.graph import GraphBackend
from igctl.backends.private import PrivateBackend
from igctl.cli.main import app
from igctl.core.config import Account
from igctl.core.context import make_backend

runner = CliRunner()


def _command_paths(cmd: object, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths = [prefix]
    for name, sub in getattr(cmd, "commands", {}).items():
        paths += _command_paths(sub, (*prefix, name))
    return paths


COMMANDS = _command_paths(typer.main.get_command(app))


def test_all_groups_registered() -> None:
    groups = {p[0] for p in COMMANDS if len(p) == 1}
    assert groups == {
        "auth",
        "post",
        "schedule",
        "analyze",
        "engage",
        "dm",
        "download",
        "automate",
    }
    assert len(COMMANDS) > 60


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: " ".join(p) or "igctl")
def test_help_renders(path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


# --- backend contract ------------------------------------------------------------------

# Methods each backend must implement itself (i.e. not inherit the NotSupported default).
GRAPH_METHODS = {
    "me",
    "publish",
    "delete_media",
    "get_profile",
    "list_media",
    "get_media",
    "list_comments",
    "hashtag_media",
    "account_insights",
    "media_insights",
    "comment",
    "delete_comment",
    "hide_comment",
}
PRIVATE_METHODS = {
    name
    for name, fn in vars(Backend).items()
    if callable(fn) and not name.startswith("_") and name not in ("hide_comment", "supports")
}


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Account:
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))
    monkeypatch.setenv("IGCTL_SMOKE_TOKEN", "fake-token")
    monkeypatch.setenv("IGCTL_SMOKE_PASSWORD", "fake-password")
    return Account(name="smoke", username="smoke", ig_user_id="1784")


def test_graph_backend_offline(secrets: Account) -> None:
    be = make_backend("graph", secrets)
    assert isinstance(be, GraphBackend) and be.name == "graph"
    assert be.token == "fake-token"
    assert be.ig_user_id == "1784"
    with pytest.raises(NotSupported):
        be.like("1")


def test_private_backend_offline(secrets: Account) -> None:
    be = make_backend("private", secrets)
    assert isinstance(be, PrivateBackend) and be.name == "private"
    assert be.session_path.parent.is_dir()


@pytest.mark.parametrize(
    ("cls", "methods"), [(GraphBackend, GRAPH_METHODS), (PrivateBackend, PRIVATE_METHODS)]
)
def test_backend_implements_contract(cls: type[Backend], methods: set[str]) -> None:
    assert not inspect.isabstract(cls)
    missing = {m for m in methods if getattr(cls, m) is getattr(Backend, m, None)}
    assert not missing, f"{cls.__name__} does not implement {sorted(missing)}"
    for name in methods & set(vars(Backend)):
        base_params = list(inspect.signature(getattr(Backend, name)).parameters)
        impl_params = list(inspect.signature(getattr(cls, name)).parameters)
        assert impl_params[: len(base_params)] == base_params, f"{cls.__name__}.{name}"


def test_private_extras_used_by_cli() -> None:
    for name in ("login", "media_pk_from_url", "download_stories", "media_likers"):
        assert callable(getattr(PrivateBackend, name))
