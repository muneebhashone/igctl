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


def _wrap(users: list[dict[str, str]], limit: int) -> dict[str, Any]:
    """Stored snapshot: the users plus whether the list is complete (not cut by ``limit``)."""
    return {"users": users, "limit": limit, "complete": limit <= 0 or len(users) < limit}


def _unwrap(data: Any) -> tuple[list[dict], bool]:
    """``(users, complete)`` of a stored snapshot; legacy snapshots were bare lists."""
    if isinstance(data, dict):
        return list(data.get("users") or []), bool(data.get("complete", True))
    return list(data or []), True


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
    *,
    complete: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Compare the current lists with the previous snapshot (either may be missing).

    Returns lists of ``{"id", "username"}`` under: ``new_followers``, ``lost_followers``,
    ``not_following_back`` (you follow them, they don't follow you), ``fans`` (they
    follow you, you don't follow them), ``new_following`` and ``unfollowed``.

    ``complete`` flags which lists are complete (keys ``followers``, ``following``,
    ``prev_followers``, ``prev_following``; missing = complete). Set differences against a
    list truncated by ``--limit`` would report users who merely fell out of the window, so
    such categories are left out and named under ``"skipped"``.
    """
    ok = {k: True for k in ("followers", "following", "prev_followers", "prev_following")}
    ok |= complete or {}
    cur_fr, cur_fg = _index(followers), _index(following)
    out: dict[str, Any] = {}
    skipped: list[str] = []

    def put(key: str, needs: tuple[str, ...], users: Any) -> None:
        if all(ok[n] for n in needs):
            out[key] = users()
        else:
            skipped.append(key)

    if prev_followers is not None:
        old = _index(prev_followers)
        both = ("followers", "prev_followers")
        put("new_followers", both, lambda: _users(cur_fr.keys() - old.keys(), cur_fr))
        put("lost_followers", both, lambda: _users(old.keys() - cur_fr.keys(), old))
    if following is not None:
        both = ("followers", "following")
        put("not_following_back", both, lambda: _users(cur_fg.keys() - cur_fr.keys(), cur_fg))
        put("fans", both, lambda: _users(cur_fr.keys() - cur_fg.keys(), cur_fr))
        if prev_following is not None:
            old_fg = _index(prev_following)
            both = ("following", "prev_following")
            put("new_following", both, lambda: _users(cur_fg.keys() - old_fg.keys(), cur_fg))
            put("unfollowed", both, lambda: _users(old_fg.keys() - cur_fg.keys(), old_fg))
    if skipped:
        out["skipped"] = skipped
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
    The ``limit`` is stored with each list so diffs know whether it was truncated.
    """
    subject = username or backend.me().username
    fr = to_snapshot(backend.followers(username, limit=limit))
    store.save_snapshot(account, FOLLOWERS, subject, _wrap(fr, limit))
    out: dict[str, Any] = {"username": subject, "followers": len(fr)}
    if include_following:
        fg = to_snapshot(backend.following(username, limit=limit))
        store.save_snapshot(account, FOLLOWING, subject, _wrap(fg, limit))
        out["following"] = len(fg)
    return out


def diff_snapshots(store: Store, account: str, username: str) -> dict[str, Any]:
    """Diff the latest stored snapshots against the ones before them."""
    fr_rows = store.snapshots(account, FOLLOWERS, username, limit=2)
    if not fr_rows:
        return {}
    fr = [_unwrap(s["data"]) for s in fr_rows]
    fg = [_unwrap(s["data"]) for s in store.snapshots(account, FOLLOWING, username, limit=2)]
    at = [s["at"] for s in fr_rows]
    complete = {
        "followers": fr[0][1],
        "following": fg[0][1] if fg else True,
        "prev_followers": fr[1][1] if len(fr) > 1 else True,
        "prev_following": fg[1][1] if len(fg) > 1 else True,
    }
    result = diff(
        fr[0][0],
        fg[0][0] if fg else None,
        fr[1][0] if len(fr) > 1 else None,
        fg[1][0] if len(fg) > 1 else None,
        complete=complete,
    )
    result["snapshot_at"] = at[0]
    result["previous_at"] = at[1] if len(at) > 1 else None
    return result


def summary(result: dict[str, Any]) -> dict[str, Any]:
    """Counts per diff category, for a compact headline."""
    return {k: (len(v) if isinstance(v, list) else v) for k, v in result.items()}
