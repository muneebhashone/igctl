"""SQLite persistence: action log, scheduled posts, follower/profile snapshots."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from igctl.core.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    action TEXT NOT NULL,          -- like, comment, follow, unfollow, dm, publish, ...
    target TEXT,                   -- media id / username / thread id
    ok INTEGER NOT NULL DEFAULT 1,
    detail TEXT,
    at TEXT NOT NULL               -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS ix_actions_acct_at ON actions(account, action, at);

CREATE TABLE IF NOT EXISTS scheduled_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    request TEXT NOT NULL,         -- PublishRequest JSON
    run_at TEXT NOT NULL,          -- ISO-8601 UTC
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | cancelled
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sched_status_run ON scheduled_posts(status, run_at);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    kind TEXT NOT NULL,            -- profile | followers | following | media
    subject TEXT NOT NULL,         -- username the snapshot is about
    data TEXT NOT NULL,            -- JSON
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(account, kind, subject, at);

CREATE TABLE IF NOT EXISTS seen (
    account TEXT NOT NULL,
    namespace TEXT NOT NULL,       -- e.g. 'autoreply:comment', 'welcome:follower'
    key TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (account, namespace, key)
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).isoformat()


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or db_path()
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- action log ----------------------------------------------------------
    def log_action(
        self, account: str, action: str, target: str | None, ok: bool = True, detail: str = ""
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO actions(account, action, target, ok, detail, at) VALUES (?,?,?,?,?,?)",
                (account, action, target, int(ok), detail, iso()),
            )

    def count_actions_since(self, account: str, action: str, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM actions WHERE account=? AND action=? AND ok=1 AND at>=?",
            (account, action, iso(since)),
        ).fetchone()
        return int(row[0])

    def recent_actions(self, account: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if account:
            q = "SELECT * FROM actions WHERE account=? ORDER BY id DESC LIMIT ?"
            return list(self._conn.execute(q, (account, limit)))
        return list(self._conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)))

    # --- schedule queue ------------------------------------------------------
    def enqueue_post(self, account: str, request_json: str, run_at: datetime) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO scheduled_posts(account, request, run_at, created_at) "
                "VALUES (?,?,?,?)",
                (account, request_json, iso(run_at), iso()),
            )
            return int(cur.lastrowid)

    def due_posts(self, now: datetime | None = None) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM scheduled_posts WHERE status='pending' AND run_at<=? "
                "ORDER BY run_at",
                (iso(now),),
            )
        )

    def claim_post(self, post_id: int) -> bool:
        """Atomically move a pending post to running. False if someone else took it."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE scheduled_posts SET status='running', attempts=attempts+1 "
                "WHERE id=? AND status='pending'",
                (post_id,),
            )
            return cur.rowcount == 1

    def finish_post(
        self, post_id: int, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE scheduled_posts SET status=?, result=?, error=? WHERE id=?",
                (status, result, error, post_id),
            )

    def list_posts(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            q = "SELECT * FROM scheduled_posts WHERE status=? ORDER BY run_at"
            return list(self._conn.execute(q, (status,)))
        return list(self._conn.execute("SELECT * FROM scheduled_posts ORDER BY run_at"))

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM scheduled_posts WHERE id=?", (post_id,)).fetchone()

    # --- snapshots -----------------------------------------------------------
    def save_snapshot(self, account: str, kind: str, subject: str, data: object) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO snapshots(account, kind, subject, data, at) VALUES (?,?,?,?,?)",
                (account, kind, subject, json.dumps(data, default=str), iso()),
            )
            return int(cur.lastrowid)

    def snapshots(self, account: str, kind: str, subject: str, limit: int = 2) -> list[dict]:
        rows = self._conn.execute(
            "SELECT data, at FROM snapshots WHERE account=? AND kind=? AND subject=? "
            "ORDER BY id DESC LIMIT ?",
            (account, kind, subject, limit),
        )
        return [{"at": r["at"], "data": json.loads(r["data"])} for r in rows]

    # --- dedup for automations -----------------------------------------------
    def mark_seen(self, account: str, namespace: str, key: str) -> bool:
        """Record ``key``; returns True if it was new."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO seen(account, namespace, key, at) VALUES (?,?,?,?)",
                (account, namespace, key, iso()),
            )
            return cur.rowcount == 1

    def is_seen(self, account: str, namespace: str, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen WHERE account=? AND namespace=? AND key=?",
            (account, namespace, key),
        ).fetchone()
        return row is not None
