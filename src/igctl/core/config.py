"""Configuration, accounts and on-disk paths.

Everything lives under ``IGCTL_HOME`` (default ``~/.igctl``)::

    ~/.igctl/config.yaml          accounts + defaults
    ~/.igctl/igctl.db             sqlite: action log, schedule queue, snapshots
    ~/.igctl/sessions/<acct>.json instagrapi session settings (private backend)

Secrets (Graph access tokens, Instagram passwords) are stored in the OS keyring under
service ``igctl`` with key ``<account>:<kind>``; they are never written to config.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import keyring
import yaml
from pydantic import BaseModel, Field

KEYRING_SERVICE = "igctl"

BackendName = Literal["graph", "private"]


def home() -> Path:
    path = Path(os.environ.get("IGCTL_HOME", Path.home() / ".igctl"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir() -> Path:
    path = home() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return home() / "igctl.db"


class Limits(BaseModel):
    """Per-account safety limits applied to automated actions (private backend)."""

    min_delay: float = 8.0  # seconds between consecutive actions
    max_delay: float = 25.0
    daily: dict[str, int] = Field(
        default_factory=lambda: {
            "like": 150,
            "comment": 40,
            "follow": 60,
            "unfollow": 80,
            "dm": 30,
            "publish": 25,
        }
    )


class Account(BaseModel):
    name: str  # local alias, e.g. "brand"
    backend: BackendName = "graph"
    username: str | None = None
    # Graph API
    ig_user_id: str | None = None
    graph_version: str = "v23.0"
    graph_host: str = "graph.facebook.com"  # or graph.instagram.com for IG Login tokens
    # Private API
    proxy: str | None = None
    limits: Limits = Field(default_factory=Limits)


class Config(BaseModel):
    default_account: str | None = None
    accounts: dict[str, Account] = Field(default_factory=dict)
    # For Graph publishing of local files: where to host them so Instagram can fetch.
    # One of: null (require --url), or an uploader spec like "s3://bucket/prefix",
    # "imgbb:<api-key-env>", or "cmd:<shell command printing a URL, {path} placeholder>".
    media_host: str | None = None

    @classmethod
    def path(cls) -> Path:
        return home() / "config.yaml"

    @classmethod
    def load(cls) -> Config:
        p = cls.path()
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self) -> None:
        self.path().write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
        )

    def account(self, name: str | None = None) -> Account:
        from igctl.backends.base import IgctlError

        name = name or os.environ.get("IGCTL_ACCOUNT") or self.default_account
        if not name:
            raise IgctlError("No account selected. Run `igctl auth login` first.")
        if name not in self.accounts:
            raise IgctlError(f"Unknown account '{name}'. See `igctl auth list`.")
        return self.accounts[name]


def get_secret(account: str, kind: str) -> str | None:
    env = os.environ.get(f"IGCTL_{account.upper()}_{kind.upper()}")
    if env:
        return env
    return keyring.get_password(KEYRING_SERVICE, f"{account}:{kind}")


def set_secret(account: str, kind: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, f"{account}:{kind}", value)


def delete_secret(account: str, kind: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, f"{account}:{kind}")
    except keyring.errors.PasswordDeleteError:
        pass
