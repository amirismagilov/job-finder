# region MODULE_CONTRACT [DOMAIN(10): AutonomousState, Audit; CONCEPT(10): Dedupe, Consent, HardStop; TECH(9): SQLite]
## @file storage.py
## @brief Private local state, migrations, deduplication and redacted audit.
## @modulecontract
## @purpose Persist one-time consent and deterministic action state so crashes, concurrent cycles and repeated cycles cannot duplicate or overrun writes.
## @scope SQLite schema, atomic policy reservations, persistent chat outbox, state transitions and counters.
## @input Non-secret action metadata and generated text.
## @output Consent, safety and deduplication queries.
## @invariants Database and directory are private; every remote write owns a SQLite reservation created only after an in-transaction policy check; credentials and transport payloads are never accepted by audit methods.
## @rationale
## Q: Why reserve writes in SQLite instead of checking counters in the worker?
## A: BEGIN IMMEDIATE serializes consent, hard-stop, interval and quota decisions across processes, while the chat outbox preserves one idempotency UUID across crash recovery.
## @changes LAST_CHANGE: [v0.2.1 — Added transactional write reservations and crash-safe recruiter-message outbox.]
## @modulemap
## CLASS 10[SQLite source of truth for autonomous policy] => Storage
## CLASS 9[Approved write attempt metadata] => WriteReservation
## CLASS 10[Stable crash-recovery chat send metadata] => ChatOutbox
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: SQLite, autonomy opt-in, hard stop, transactional write reservation, persistent chat outbox, idempotency, dedupe, audit
# STRUCTURE: migrate -> BEGIN IMMEDIATE policy gate -> reserve capacity/outbox UUID -> remote receipt -> atomic processed/action commit

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid


class StorageError(RuntimeError):
    pass


class WriteGuardError(StorageError):
    """A deterministic policy reason rejected a remote write reservation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WriteReservation:
    reservation_id: str
    kind: str
    target: str


@dataclass(frozen=True)
class ChatOutbox:
    recruiter_message_id: str
    chat_id: str
    idempotency_key: str
    response_text: str
    input_hash: str
    state: str
    response_message_id: str | None = None


SENSITIVE_KEYS = {"cookie", "cookies", "authorization", "csrf", "xsrf", "pairing_secret", "api_key", "token", "headers"}


def _safe_detail(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_detail(v) for k, v in value.items() if str(k).lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [_safe_detail(v) for v in value[:100]]
    if isinstance(value, str):
        lowered = value.lower()
        if "bearer " in lowered or "set-cookie:" in lowered:
            return "[REDACTED]"
        return value[:10_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


# region CLASS_Storage [DOMAIN(10): AutonomousState; CONCEPT(10): TransactionalSafety; TECH(9): SQLite]
## @purpose Provide crash-safe policy, idempotency and local observability across autonomous cycles.
class Storage:
    SCHEMA_VERSION = 3

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
                CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
                INSERT INTO schema_meta(version) SELECT 3 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);
                UPDATE schema_meta SET version = 3;
                CREATE TABLE IF NOT EXISTS safety_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1), hard_stop INTEGER NOT NULL DEFAULT 0,
                    reason TEXT, created_at TEXT
                );
                INSERT OR IGNORE INTO safety_state(id, hard_stop) VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS autonomy_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1), enabled INTEGER NOT NULL DEFAULT 0,
                    enabled_at TEXT, disabled_at TEXT
                );
                INSERT OR IGNORE INTO autonomy_state(id, enabled) VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS action_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT NOT NULL,
                    created_at TEXT NOT NULL, success INTEGER NOT NULL, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_action_day ON action_events(kind, created_at, success);
                CREATE TABLE IF NOT EXISTS vacancy_decisions (
                    vacancy_id TEXT PRIMARY KEY, decided_at TEXT NOT NULL, status TEXT NOT NULL,
                    score INTEGER, input_hash TEXT NOT NULL, reason TEXT, generated_text TEXT
                );
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, processed_at TEXT NOT NULL,
                    response_message_id TEXT NOT NULL, input_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS write_reservations (
                    reservation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
                    created_at TEXT NOT NULL, last_attempt_at TEXT NOT NULL, completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('reserved','sent','failed')),
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(kind, target)
                );
                CREATE INDEX IF NOT EXISTS idx_write_reservation_day
                    ON write_reservations(kind, created_at, status);
                CREATE TABLE IF NOT EXISTS chat_outbox (
                    recruiter_message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE, response_text TEXT NOT NULL,
                    input_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('sending','pending','sent')),
                    response_message_id TEXT, created_at TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
                    completed_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_chat_outbox_day
                    ON chat_outbox(created_at, state);
                CREATE TABLE IF NOT EXISTS bridge_commands (
                    command_id TEXT PRIMARY KEY, action TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, outcome TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, importance INTEGER NOT NULL,
                    event TEXT NOT NULL, target_hash TEXT, detail_json TEXT NOT NULL
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def set_autonomy(self, enabled: bool) -> None:
        now = self.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE autonomy_state SET enabled=?, enabled_at=CASE WHEN ? THEN ? ELSE enabled_at END, disabled_at=CASE WHEN ? THEN disabled_at ELSE ? END WHERE id=1",
                (int(enabled), int(enabled), now, int(enabled), now),
            )
        self.audit(10, "autonomy_enabled" if enabled else "autonomy_disabled", detail={"enabled": enabled})

    def autonomy_enabled(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT enabled FROM autonomy_state WHERE id=1").fetchone()
            return bool(row and row["enabled"])

    def hard_stop(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT hard_stop, reason FROM safety_state WHERE id=1").fetchone()
            return str(row["reason"]) if row and row["hard_stop"] else None

    def set_hard_stop(self, reason: str) -> None:
        safe = str(_safe_detail(reason))[:1000]
        with self._connect() as conn:
            conn.execute("UPDATE safety_state SET hard_stop=1, reason=?, created_at=? WHERE id=1", (safe, self.now().isoformat()))
        self.audit(10, "hard_stop", detail={"reason": safe})

    def clear_hard_stop(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE safety_state SET hard_stop=0, reason=NULL, created_at=NULL WHERE id=1")
        self.audit(10, "hard_stop_cleared")

    @staticmethod
    def _last_attempt_in_transaction(conn: sqlite3.Connection) -> datetime | None:
        row = conn.execute(
            "SELECT attempted_at FROM ("
            "SELECT last_attempt_at AS attempted_at FROM write_reservations "
            "UNION ALL SELECT last_attempt_at AS attempted_at FROM chat_outbox "
            "UNION ALL SELECT created_at AS attempted_at FROM action_events WHERE success=1"
            ") ORDER BY attempted_at DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(str(row["attempted_at"])) if row else None

    @staticmethod
    def _assert_write_policy(
        conn: sqlite3.Connection,
        *,
        kind: str,
        now: datetime,
        daily_limit: int,
        minimum_interval_seconds: float,
        existing_target: str | None = None,
    ) -> None:
        """policy state + reserved capacity + cadence -> approval or stable denial reason.

        This helper runs only inside a caller-owned ``BEGIN IMMEDIATE`` transaction. It intentionally
        counts in-flight reservations as quota consumption, preventing two workers from both observing
        spare capacity, and treats the reservation timestamp as the start of a remote write attempt.
        """
        policy = conn.execute(
            "SELECT a.enabled, s.hard_stop FROM autonomy_state a CROSS JOIN safety_state s WHERE a.id=1 AND s.id=1"
        ).fetchone()
        if not policy or not bool(policy["enabled"]):
            raise WriteGuardError("autonomy_disabled")
        if bool(policy["hard_stop"]):
            raise WriteGuardError("hard_stop")
        day = now.date().isoformat()
        successful = conn.execute(
            "SELECT COUNT(*) AS n FROM action_events WHERE kind=? AND success=1 AND substr(created_at,1,10)=?",
            (kind, day),
        ).fetchone()["n"]
        if kind == "chat_message":
            sql = "SELECT COUNT(*) AS n FROM chat_outbox WHERE substr(created_at,1,10)=? AND state='sending'"
            params: tuple[Any, ...] = (day,)
            if existing_target is not None:
                sql += " AND recruiter_message_id<>?"
                params += (existing_target,)
        else:
            sql = "SELECT COUNT(*) AS n FROM write_reservations WHERE substr(created_at,1,10)=? AND status='reserved' AND kind=?"
            params = (day, kind)
            if existing_target is not None:
                sql += " AND target<>?"
                params += (existing_target,)
        active = conn.execute(sql, params).fetchone()["n"]
        # BUG_FIX_CONTEXT: Separate counter reads allowed concurrent workers to pass the same daily limit;
        # BEGIN IMMEDIATE plus counting in-flight reservations makes quota ownership transactional.
        if int(successful) + int(active) >= max(0, int(daily_limit)):
            raise WriteGuardError(f"{kind}_daily_limit")
        last = Storage._last_attempt_in_transaction(conn)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < max(0.0, float(minimum_interval_seconds)):
                raise WriteGuardError("minimum_write_interval")

    # region METHOD_reserve_write [DOMAIN(10): AutonomousState; CONCEPT(10): AtomicPolicyReservation; TECH(9): SQLite]
    ## @purpose Start a non-idempotent remote write only if consent, hard-stop, quota and cadence all pass in one serialized transaction.
    ## @io kind, target, limits -> WriteReservation or WriteGuardError
    ## @complexity 9
    def reserve_write(self, kind: str, target: str, *, daily_limit: int, minimum_interval_seconds: float) -> WriteReservation:
        """BEGIN IMMEDIATE -> re-read policy -> reserve capacity and attempt timestamp -> COMMIT."""
        now = self.now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT reservation_id,status,last_attempt_at FROM write_reservations WHERE kind=? AND target=?",
                    (kind, target),
                ).fetchone()
                if existing and existing["status"] in {"reserved", "sent"}:
                    raise WriteGuardError(f"{kind}_already_reserved")
                self._assert_write_policy(
                    conn,
                    kind=kind,
                    now=now,
                    daily_limit=daily_limit,
                    minimum_interval_seconds=minimum_interval_seconds,
                )
                reservation_id = str(existing["reservation_id"]) if existing else str(uuid.uuid4())
                if existing:
                    conn.execute(
                        "UPDATE write_reservations SET created_at=?,last_attempt_at=?,completed_at=NULL,status='reserved',attempt_count=attempt_count+1 WHERE reservation_id=?",
                        (now.isoformat(), now.isoformat(), reservation_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO write_reservations(reservation_id,kind,target,created_at,last_attempt_at,status) VALUES(?,?,?,?,?,'reserved')",
                        (reservation_id, kind, target, now.isoformat(), now.isoformat()),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return WriteReservation(reservation_id, kind, target)
    # endregion METHOD_reserve_write

    def finish_write(self, reservation: WriteReservation, *, success: bool, detail: dict[str, Any] | None = None) -> None:
        safe = json.dumps(_safe_detail(detail or {}), ensure_ascii=False)
        now = self.now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT status FROM write_reservations WHERE reservation_id=? AND kind=? AND target=?",
                    (reservation.reservation_id, reservation.kind, reservation.target),
                ).fetchone()
                if not row or row["status"] != "reserved":
                    raise StorageError("Write reservation отсутствует или уже завершён")
                conn.execute(
                    "UPDATE write_reservations SET status=?,completed_at=? WHERE reservation_id=?",
                    ("sent" if success else "failed", now, reservation.reservation_id),
                )
                conn.execute(
                    "INSERT INTO action_events(kind,target,created_at,success,detail) VALUES(?,?,?,?,?)",
                    (reservation.kind, reservation.target, now, int(success), safe),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def chat_outbox(self, recruiter_message_id: str) -> ChatOutbox | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recruiter_message_id,chat_id,idempotency_key,response_text,input_hash,state,response_message_id FROM chat_outbox WHERE recruiter_message_id=?",
                (recruiter_message_id,),
            ).fetchone()
        return ChatOutbox(**dict(row)) if row else None

    # region METHOD_reserve_chat_send [DOMAIN(10): RecruiterChat; CONCEPT(10): PersistentIdempotentOutbox; TECH(9): SQLite, UUID]
    ## @purpose Persist one immutable idempotency key and reply before a recruiter-message send, while atomically rechecking all write policy.
    ## @io message/chat/text/hash/limits -> ChatOutbox or WriteGuardError
    ## @complexity 10
    def reserve_chat_send(
        self,
        recruiter_message_id: str,
        chat_id: str,
        response_text: str,
        input_hash: str,
        *,
        daily_limit: int,
        minimum_interval_seconds: float,
    ) -> ChatOutbox:
        """serialized policy gate -> create-or-reuse stable UUID outbox -> mark sending -> return persisted payload.

        A process crash after the HTTP response leaves the row in ``sending``. The next cycle reuses both
        text and UUID, allowing the remote idempotency contract to reconcile the same logical message.
        """
        now = self.now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute("SELECT * FROM chat_outbox WHERE recruiter_message_id=?", (recruiter_message_id,)).fetchone()
                if existing and existing["state"] == "sent":
                    raise WriteGuardError("message_deduplicated")
                if existing and str(existing["chat_id"]) != chat_id:
                    raise StorageError("Outbox chat_id не совпадает с исходным сообщением")
                # BUG_FIX_CONTEXT: Generating a UUID inside HHWebClient lost it on crash after remote send;
                # the UUID and exact text now exist transactionally before any network call and are reused.
                self._assert_write_policy(
                    conn,
                    kind="chat_message",
                    now=now,
                    daily_limit=daily_limit,
                    minimum_interval_seconds=minimum_interval_seconds,
                    existing_target=recruiter_message_id if existing else None,
                )
                if existing:
                    conn.execute(
                        "UPDATE chat_outbox SET state='sending',last_attempt_at=?,attempt_count=attempt_count+1 WHERE recruiter_message_id=?",
                        (now.isoformat(), recruiter_message_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO chat_outbox(recruiter_message_id,chat_id,idempotency_key,response_text,input_hash,state,created_at,last_attempt_at) VALUES(?,?,?,?,?,'sending',?,?)",
                        (recruiter_message_id, chat_id, str(uuid.uuid4()), response_text, input_hash, now.isoformat(), now.isoformat()),
                    )
                row = conn.execute(
                    "SELECT recruiter_message_id,chat_id,idempotency_key,response_text,input_hash,state,response_message_id FROM chat_outbox WHERE recruiter_message_id=?",
                    (recruiter_message_id,),
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return ChatOutbox(**dict(row))
    # endregion METHOD_reserve_chat_send

    def complete_chat_send(self, recruiter_message_id: str, response_message_id: str) -> None:
        now = self.now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM chat_outbox WHERE recruiter_message_id=?", (recruiter_message_id,)).fetchone()
                if not row:
                    raise StorageError("Outbox не найден при фиксации receipt")
                if row["state"] != "sent":
                    conn.execute(
                        "UPDATE chat_outbox SET state='sent',response_message_id=?,completed_at=? WHERE recruiter_message_id=?",
                        (response_message_id, now, recruiter_message_id),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_messages(message_id,chat_id,processed_at,response_message_id,input_hash) VALUES(?,?,?,?,?)",
                        (recruiter_message_id, row["chat_id"], now, response_message_id, row["input_hash"]),
                    )
                    conn.execute(
                        "INSERT INTO action_events(kind,target,created_at,success,detail) VALUES('chat_message',?,?,1,?)",
                        (recruiter_message_id, now, json.dumps({"response_message_id": response_message_id}, ensure_ascii=False)),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def defer_chat_send(self, recruiter_message_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_outbox SET state='pending' WHERE recruiter_message_id=? AND state='sending'",
                (recruiter_message_id,),
            )

    def record_vacancy(self, vacancy_id: str, status: str, *, score: int | None, input_hash: str, reason: str = "", generated_text: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO vacancy_decisions(vacancy_id,decided_at,status,score,input_hash,reason,generated_text) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(vacancy_id) DO UPDATE SET decided_at=excluded.decided_at,status=excluded.status,score=excluded.score,input_hash=excluded.input_hash,reason=excluded.reason,generated_text=excluded.generated_text",
                (vacancy_id, self.now().isoformat(), status, score, input_hash, reason[:1000], generated_text),
            )

    def vacancy_status(self, vacancy_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM vacancy_decisions WHERE vacancy_id=?", (vacancy_id,)).fetchone()
            return str(row["status"]) if row else None

    def message_processed(self, message_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM processed_messages WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_message_processed(self, message_id: str, chat_id: str, response_message_id: str, input_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages(message_id,chat_id,processed_at,response_message_id,input_hash) VALUES(?,?,?,?,?)",
                (message_id, chat_id, self.now().isoformat(), response_message_id, input_hash),
            )
            conn.execute("COMMIT")

    def record_action(self, kind: str, target: str, *, success: bool, detail: dict[str, Any] | None = None) -> None:
        safe = json.dumps(_safe_detail(detail or {}), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("INSERT INTO action_events(kind,target,created_at,success,detail) VALUES(?,?,?,?,?)", (kind, target, self.now().isoformat(), int(success), safe))

    def successful_today(self, kind: str) -> int:
        day = self.now().date().isoformat()
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM action_events WHERE kind=? AND success=1 AND substr(created_at,1,10)=?", (kind, day)).fetchone()
            return int(row["n"])

    def last_action_time(self, kinds: tuple[str, ...]) -> datetime | None:
        marks = ",".join("?" for _ in kinds)
        with self._connect() as conn:
            row = conn.execute(f"SELECT created_at FROM action_events WHERE kind IN ({marks}) AND success=1 ORDER BY id DESC LIMIT 1", kinds).fetchone()
            return datetime.fromisoformat(row["created_at"]) if row else None

    def record_bridge_command(self, command_id: str, action: str, outcome: str) -> None:
        now = self.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bridge_commands(command_id,action,created_at,updated_at,outcome) VALUES(?,?,?,?,?) "
                "ON CONFLICT(command_id) DO UPDATE SET updated_at=excluded.updated_at,outcome=excluded.outcome",
                (command_id, action, now, now, outcome),
            )

    def audit(self, importance: int, event: str, *, target: str | None = None, detail: dict[str, Any] | None = None) -> None:
        safe = json.dumps(_safe_detail(detail or {}), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_events(created_at,importance,event,target_hash,detail_json) VALUES(?,?,?,?,?)",
                (self.now().isoformat(), max(1, min(importance, 10)), event[:100], self.digest(target) if target else None, safe),
            )

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT created_at,importance,event,target_hash,detail_json FROM audit_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
            return [{"created_at": row["created_at"], "importance": row["importance"], "event": row["event"], "target_hash": row["target_hash"], "detail": json.loads(row["detail_json"])} for row in rows]
# endregion CLASS_Storage
