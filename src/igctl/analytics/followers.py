"""Follower/following snapshots and diffs (new followers, unfollowers, fans...)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from igctl.backends.base import Backend
from igctl.core.models import UserSummary
from igctl.core.storage import Store

FOLLOWERS = "followers"
FOLLOWING = "following"


def to_snapshot(users: Iterable[UserSummary]) -> list[dict[str, str]]:
    """Compact, JSON-friendly form stored in the snapshots table."""
    return [{"id": u.id, "username": u.username} for u in users]


def _index(snapshot: list[dict] | None) -> dict[str, str]:
    """id -> username. User ids are stable while usernames can change."""
    return {str(u["id"]): u["username"] for u in snapshot or []}


def _users(ids: Iterable[str], names: dict[str, str]) -> list[dict[str, str]]:
    return sorted(({"id": i, "username": names[i]} for i in ids), key=lambda u: u["username"])


def diff(
    followers: list[dict],
    following: list[dict] | None = None,
    prev_followers: list[dict] | None = None,
    prev_following: list[dict] | None = None,
) -> dict[str, Any]:
    """Compare the current lists with the previous snapshot (either may be missing).

    Returns lists of ``{"id", "username"}`` under: ``new_followers``, ``lost_followers``,
    ``not_following_back`` (you follow them, they don't follow you), ``fans`` (they
    follow you, you don't follow them), ``new_following`` and ``unfollowed``.
    """
    cur_fr, cur_fg = _index(followers), _index(following)
    out: dict[str, Any] = {}
    if prev_followers is not None:
        old = _index(prev_followers)
        out["new_followers"] = _users(cur_fr.keys() - old.keys(), cur_fr)
        out["lost_followers"] = _users(old.keys() - cur_fr.keys(), old)
    if following is not None:
        out["not_following_back"] = _users(cur_fg.keys() - cur_fr.keys(), cur_fg)
        out["fans"] = _users(cur_fr.keys() - cur_fg.keys(), cur_fr)
        if prev_following is not None:
            old = _index(prev_following)
            out["new_following"] = _users(cur_fg.keys() - old.keys(), cur_fg)
            out["unfollowed"] = _users(old.keys() - cur_fg.keys(), old)
    return out


def take_snapshot(
    backend: Backend,
    store: Store,
    account: str,
    username: str | None = None,
    limit: int = 0,
    include_following: bool = True,
) -> dict[str, Any]:
    """Fetch and store the follower (and following) lists of ``username`` (None = me).

    Returns ``{"username", "followers", "following"}`` with the resolved handle and counts.
    """
    subject = username or backend.me().username
    fr = to_snapshot(backend.followers(username, limit=limit))
    store.save_snapshot(account, FOLLOWERS, subject, fr)
    out: dict[str, Any] = {"username": subject, "followers": len(fr)}
    if include_following:
        fg = to_snapshot(backend.following(username, limit=limit))
        store.save_snapshot(account, FOLLOWING, subject, fg)
        out["following"] = len(fg)
    return out


def diff_snapshots(store: Store, account: str, username: str) -> dict[str, Any]:
    """Diff the latest stored snapshots against the ones before them."""
    fr = store.snapshots(account, FOLLOWERS, username, limit=2)
    fg = store.snapshots(account, FOLLOWING, username, limit=2)
    if not fr:
        return {}
    result = diff(
        fr[0]["data"],
        fg[0]["data"] if fg else None,
        fr[1]["data"] if len(fr) > 1 else None,
        fg[1]["data"] if len(fg) > 1 else None,
    )
    result["snapshot_at"] = fr[0]["at"]
    result["previous_at"] = fr[1]["at"] if len(fr) > 1 else None
    return result


def summary(result: dict[str, Any]) -> dict[str, Any]:
    """Counts per diff category, for a compact headline."""
    return {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}
