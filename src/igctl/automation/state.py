"""Automation state kept in the shared igctl sqlite database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from igctl.core.storage import Store, iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_runs (
    account TEXT NOT NULL,
    rule TEXT NOT NULL,
    last_run TEXT NOT NULL,        -- ISO-8601 UTC
    status TEXT NOT NULL,
    summary TEXT,
    PRIMARY KEY (account, rule)
);
CREATE TABLE IF NOT EXISTS automation_halts (
    account TEXT PRIMARY KEY,
    until TEXT NOT NULL,           -- ISO-8601 UTC
    reason TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class LastRun:
    at: datetime
    status: str
    summary: str | None


class RuleState:
    """Last-run bookkeeping per (account, rule)."""

    def __init__(self, store: Store):
        self.store = store
        with store.tx() as c:
            c.executescript(_SCHEMA)

    def last(self, account: str, rule: str) -> LastRun | None:
        with self.store.tx() as c:
            row = c.execute(
                "SELECT last_run, status, summary FROM automation_runs WHERE account=? AND rule=?",
                (account, rule),
            ).fetchone()
        if row is None:
            return None
        return LastRun(datetime.fromisoformat(row[0]), row[1], row[2])

    def record(self, account: str, rule: str, at: datetime, status: str, summary: str) -> None:
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO automation_runs(account, rule, last_run, status, summary) "
                "VALUES (?,?,?,?,?) ON CONFLICT(account, rule) DO UPDATE SET "
                "last_run=excluded.last_run, status=excluded.status, summary=excluded.summary",
                (account, rule, iso(at), status, summary),
            )

    def halt(self, account: str, until: datetime, reason: str) -> None:
        """Stop automating ``account`` until ``until`` (auth failure, action block)."""
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO automation_halts(account, until, reason) VALUES (?,?,?) "
                "ON CONFLICT(account) DO UPDATE SET until=excluded.until, reason=excluded.reason",
                (account, iso(until), reason),
            )

    def halted(self, account: str, now: datetime) -> tuple[datetime, str] | None:
        """``(until, reason)`` while ``account`` is halted, else None."""
        with self.store.tx() as c:
            row = c.execute(
                "SELECT until, reason FROM automation_halts WHERE account=?", (account,)
            ).fetchone()
        if row is None:
            return None
        until = datetime.fromisoformat(row[0])
        return (until, row[1]) if until > now else None


def last_action_at(store: Store, account: str, action: str, target: str) -> datetime | None:
    """When ``action`` last succeeded on ``target`` (e.g. when we followed a user).

    Usernames are case-insensitive on Instagram, so targets are compared that way.
    """
    with store.tx() as c:
        row = c.execute(
            "SELECT MAX(at) FROM actions WHERE account=? AND action=? AND lower(target)=lower(?) "
            "AND ok=1",
            (account, action, target),
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None
