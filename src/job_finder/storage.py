from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator


class StorageError(RuntimeError):
    pass


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','in_progress','sent','failed')),
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS action_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS safety_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    hard_stop INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    created_at TEXT
                );
                INSERT OR IGNORE INTO safety_state(id, hard_stop) VALUES (1, 0);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    def create_pending(self, kind: str, target: str, payload: dict[str, Any], ttl_minutes: int) -> str:
        action_id = str(uuid.uuid4())
        now = self.now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_actions VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
                (
                    action_id,
                    kind,
                    target,
                    json.dumps(payload, ensure_ascii=False),
                    now.isoformat(),
                    (now + timedelta(minutes=ttl_minutes)).isoformat(),
                ),
            )
        return action_id

    def claim_pending(self, action_id: str, expected_kind: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise StorageError("Подтверждение не найдено")
            if row["kind"] != expected_kind:
                conn.execute("ROLLBACK")
                raise StorageError("Тип подтверждения не соответствует действию")
            if row["status"] != "pending":
                conn.execute("ROLLBACK")
                raise StorageError(f"Подтверждение уже использовано: {row['status']}")
            if datetime.fromisoformat(row["expires_at"]) < self.now():
                conn.execute("UPDATE pending_actions SET status = 'failed', result_json = ? WHERE id = ?", (json.dumps({"error": "expired"}), action_id))
                conn.execute("COMMIT")
                raise StorageError("Подтверждение истекло; создайте новый предпросмотр")
            conn.execute("UPDATE pending_actions SET status = 'in_progress' WHERE id = ?", (action_id,))
            conn.execute("COMMIT")
            return {
                "id": row["id"],
                "kind": row["kind"],
                "target": row["target"],
                "payload": json.loads(row["payload_json"]),
            }

    def finish_pending(self, action_id: str, *, success: bool, result: dict[str, Any]) -> None:
        status = "sent" if success else "failed"
        with self._connect() as conn:
            row = conn.execute("SELECT kind, target FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise StorageError("Подтверждение не найдено при завершении")
            conn.execute(
                "UPDATE pending_actions SET status = ?, result_json = ? WHERE id = ?",
                (status, json.dumps(result, ensure_ascii=False), action_id),
            )
            conn.execute(
                "INSERT INTO action_events(kind, target, created_at, success, detail) VALUES (?, ?, ?, ?, ?)",
                (row["kind"], row["target"], self.now().isoformat(), int(success), json.dumps(result, ensure_ascii=False)),
            )

    def successful_today(self, kind: str) -> int:
        day = self.now().date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM action_events WHERE kind = ? AND success = 1 AND substr(created_at, 1, 10) = ?",
                (kind, day),
            ).fetchone()
            return int(row["n"])

    def was_sent(self, kind: str, target: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM action_events WHERE kind = ? AND target = ? AND success = 1 LIMIT 1",
                (kind, target),
            ).fetchone()
            return row is not None

    def hard_stop(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT hard_stop, reason FROM safety_state WHERE id = 1").fetchone()
            return str(row["reason"]) if row and row["hard_stop"] else None

    def set_hard_stop(self, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE safety_state SET hard_stop = 1, reason = ?, created_at = ? WHERE id = 1",
                (reason[:1000], self.now().isoformat()),
            )

    def clear_hard_stop(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE safety_state SET hard_stop = 0, reason = NULL, created_at = NULL WHERE id = 1")

    def last_action_time(self, kinds: tuple[str, ...]) -> datetime | None:
        marks = ",".join("?" for _ in kinds)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT created_at FROM action_events WHERE kind IN ({marks}) ORDER BY id DESC LIMIT 1",
                kinds,
            ).fetchone()
            return datetime.fromisoformat(row["created_at"]) if row else None
