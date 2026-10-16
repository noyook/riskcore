"""SQLite implementation of the append-only event log store (dev/test store).

Production store is PostgreSQL (see ddl/postgres/001_event_log.sql) behind the
same interface; the kernel never talks to a store directly — only the
projector and the ingest boundary do.

Determinism notes:
- timestamps are stored as UTC ISO-8601 strings;
- payload/provenance JSON is canonicalized (sorted keys, compact) so that
  golden-replay byte comparison is stable;
- seq assignment happens inside the write transaction (BEGIN IMMEDIATE),
  serialized per database file — single-writer semantics by construction.
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple, Protocol
from uuid import UUID

from .envelope import EventEnvelope, StoredEvent
from .types import EventType

DDL_PATH = Path(__file__).resolve().parent.parent / "ddl" / "sqlite" / "001_event_log.sql"


def _synchronized(fn):
    """Serialize store access across threads (uvicorn workers, TestClient)."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class AppendResult(NamedTuple):
    event_id: str
    seq: int
    duplicate: bool


class OutboxRow(NamedTuple):
    id: int
    tenant_id: str
    event_id: str
    channel: str
    payload: dict
    attempts: int


class EventLogStore(Protocol):
    """Interface shared by the SQLite dev store and the Postgres store."""

    @_synchronized
    def append(self, event: EventEnvelope) -> AppendResult: ...
    @_synchronized
    def append_batch(self, events: Iterable[EventEnvelope]) -> list[AppendResult]: ...
    @_synchronized
    def get(self, tenant_id: str, event_id: str | UUID) -> StoredEvent | None: ...
    @_synchronized
    def read_object_events(self, tenant_id: str, object_id: str, upto_seq: int | None = None) -> list[StoredEvent]: ...
    @_synchronized
    def watermark(self, tenant_id: str, object_id: str) -> int: ...
    @_synchronized
    def advance_watermark(self, tenant_id: str, object_id: str, seq: int, advanced_at: datetime) -> bool: ...
    @_synchronized
    def pending_outbox(self, limit: int = 100) -> list[OutboxRow]: ...
    @_synchronized
    def mark_delivered(self, outbox_id: int, delivered_at: datetime) -> None: ...
    @_synchronized
    def mark_dead(self, outbox_id: int, error: str) -> None: ...
    def close(self) -> None: ...


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


class AppendOnlyViolation(RuntimeError):
    """Raised when a write would mutate an existing log row (never expected)."""


class CorrectionTargetMissing(RuntimeError):
    """Raised when a correction event points at a non-existent event."""


class DuplicateSeqConflict(RuntimeError):
    """Raised when the per-object seq counter and the unique index disagree."""


class SqliteStore:
    """Append-only event log on SQLite (development / test / replay store)."""

    def __init__(self, db_path: str | Path, *, apply_ddl: bool = True) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False + RLock: the ingest boundary (TestClient /
        # uvicorn workers) touches the store from non-main threads; writes are
        # serialized by BEGIN IMMEDIATE transactions, reads are consistent
        # snapshots under WAL.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        if apply_ddl:
            self._conn.executescript(DDL_PATH.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ write

    def append(self, event: EventEnvelope) -> AppendResult:
        if event.seq is not None:
            raise ValueError("seq is server-assigned; ingest payloads must carry seq=None")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._conn.execute(
                "SELECT seq FROM event_log WHERE tenant_id=? AND event_id=?",
                (event.tenant_id, str(event.event_id)),
            ).fetchone()
            if existing is not None:
                self._conn.execute("COMMIT")
                return AppendResult(str(event.event_id), int(existing["seq"]), True)

            if event.type is EventType.CORRECTION:
                target = str(event.payload["corrects_event_id"])
                hit = self._conn.execute(
                    "SELECT 1 FROM event_log WHERE tenant_id=? AND event_id=?",
                    (event.tenant_id, target),
                ).fetchone()
                if hit is None:
                    raise CorrectionTargetMissing(
                        f"correction target {target} does not exist for tenant {event.tenant_id}"
                    )

            seq = self._next_seq(event.tenant_id, event.object_id)
            self._conn.execute(
                """
                INSERT INTO event_log (
                    tenant_id, event_id, trace_id, object_id, object_type, seq,
                    occurred_at, ingested_at, type, payload, provenance, schema_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.tenant_id,
                    str(event.event_id),
                    event.trace_id,
                    event.object_id,
                    event.object_type.value,
                    seq,
                    _iso(event.occurred_at),
                    _iso(event.ingested_at),
                    event.type.value,
                    _canonical(event.payload),
                    _canonical(event.provenance.model_dump(mode="json")),
                    event.schema_version,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO outbox (tenant_id, event_id, channel, payload, created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    event.tenant_id,
                    str(event.event_id),
                    "default",
                    _canonical({"event_id": str(event.event_id), "type": event.type.value}),
                    _iso(event.ingested_at),
                ),
            )
            self._conn.execute("COMMIT")
            return AppendResult(str(event.event_id), seq, False)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def append_batch(self, events: Iterable[EventEnvelope]) -> list[AppendResult]:
        return [self.append(e) for e in events]

    def _next_seq(self, tenant_id: str, object_id: str) -> int:
        self._conn.execute(
            "INSERT INTO object_seq (tenant_id, object_id, last_seq) VALUES (?,?,0) "
            "ON CONFLICT(tenant_id, object_id) DO NOTHING",
            (tenant_id, object_id),
        )
        row = self._conn.execute(
            "UPDATE object_seq SET last_seq = last_seq + 1 "
            "WHERE tenant_id=? AND object_id=? RETURNING last_seq",
            (tenant_id, object_id),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise DuplicateSeqConflict(f"seq counter row missing for {tenant_id}/{object_id}")
        return int(row["last_seq"])

    # ------------------------------------------------------------------- read

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent.model_validate(
            {
                "event_id": row["event_id"],
                "tenant_id": row["tenant_id"],
                "trace_id": row["trace_id"],
                "object_id": row["object_id"],
                "object_type": row["object_type"],
                "seq": row["seq"],
                "occurred_at": _parse_dt(row["occurred_at"]),
                "ingested_at": _parse_dt(row["ingested_at"]),
                "type": row["type"],
                "payload": json.loads(row["payload"]),
                "provenance": json.loads(row["provenance"]),
                "schema_version": row["schema_version"],
            }
        )

    def get(self, tenant_id: str, event_id: str | UUID) -> StoredEvent | None:
        row = self._conn.execute(
            "SELECT * FROM event_log WHERE tenant_id=? AND event_id=?",
            (tenant_id, str(event_id)),
        ).fetchone()
        return self._row_to_event(row) if row else None

    def read_object_events(
        self, tenant_id: str, object_id: str, upto_seq: int | None = None
    ) -> list[StoredEvent]:
        if upto_seq is None:
            rows = self._conn.execute(
                "SELECT * FROM event_log WHERE tenant_id=? AND object_id=? ORDER BY seq",
                (tenant_id, object_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM event_log WHERE tenant_id=? AND object_id=? AND seq<=? ORDER BY seq",
                (tenant_id, object_id, upto_seq),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @_synchronized
    def iter_objects(self, tenant_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT object_id FROM event_log WHERE tenant_id=?", (tenant_id,)
        ).fetchall()
        return sorted(r["object_id"] for r in rows)

    # -------------------------------------------------------------- watermark

    def watermark(self, tenant_id: str, object_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(seq) AS wm FROM watermark_log WHERE tenant_id=? AND object_id=?",
            (tenant_id, object_id),
        ).fetchone()
        return int(row["wm"]) if row and row["wm"] is not None else 0

    def advance_watermark(self, tenant_id: str, object_id: str, seq: int, advanced_at: datetime) -> bool:
        """Forward-only. Returns True when the watermark moved."""
        if seq <= self.watermark(tenant_id, object_id):
            return False
        self._conn.execute(
            "INSERT OR IGNORE INTO watermark_log (tenant_id, object_id, seq, advanced_at) VALUES (?,?,?,?)",
            (tenant_id, object_id, seq, _iso(advanced_at)),
        )
        return True

    # ----------------------------------------------------------------- outbox

    def pending_outbox(self, limit: int = 100) -> list[OutboxRow]:
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [
            OutboxRow(
                id=int(r["id"]),
                tenant_id=r["tenant_id"],
                event_id=r["event_id"],
                channel=r["channel"],
                payload=json.loads(r["payload"]),
                attempts=int(r["attempts"]),
            )
            for r in rows
        ]

    def mark_delivered(self, outbox_id: int, delivered_at: datetime) -> None:
        self._conn.execute(
            "UPDATE outbox SET status='delivered', delivered_at=? WHERE id=?",
            (_iso(delivered_at), outbox_id),
        )

    def mark_dead(self, outbox_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE outbox SET status='dead', last_error=? WHERE id=?", (error[:2000], outbox_id)
        )

    @_synchronized
    def bump_attempts(self, outbox_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error=? WHERE id=?",
            (error[:2000], outbox_id),
        )

    # ------------------------------------------------------------------ close

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
