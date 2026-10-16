"""Bypass observation store — 旁路观察期的"本会拒派"留痕（W-006）。

Executor-local audit table (NOT the fact log): every request that WOULD have
been denied while allow_dispatch runs in observe mode is recorded here for the
W-009 dual-run comparison. Action consequences themselves flow back to the log
as events by the caller (executor rule 6).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

EXECUTOR_DDL = Path(__file__).resolve().parent.parent / "ddl" / "sqlite" / "001_executor.sql"


@dataclass
class BypassObservation:
    object_id: str
    predicate: str
    would_intervention: str
    reason_code: str
    r_milli: int | None
    clause: str
    explain_refs: list[str]
    state_as_of: datetime | None
    observed_at: datetime
    id: int | None = None


class ObservationStore(Protocol):
    def record(self, obs: BypassObservation) -> int: ...
    def list(self, limit: int = 100) -> list[BypassObservation]: ...
    def count(self) -> int: ...


class SqliteObservationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(EXECUTOR_DDL.read_text(encoding="utf-8"))

    def record(self, obs: BypassObservation) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO bypass_observation (
                object_id, predicate, would_intervention, reason_code, r_milli,
                clause, explain_refs, state_as_of, observed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                obs.object_id,
                obs.predicate,
                obs.would_intervention,
                obs.reason_code,
                obs.r_milli,
                obs.clause,
                json.dumps(obs.explain_refs),
                obs.state_as_of.isoformat() if obs.state_as_of else None,
                obs.observed_at.isoformat(),
            ),
        )
        return int(cur.lastrowid)

    def list(self, limit: int = 100) -> list[BypassObservation]:
        rows = self._conn.execute(
            "SELECT * FROM bypass_observation ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            BypassObservation(
                id=r["id"],
                object_id=r["object_id"],
                predicate=r["predicate"],
                would_intervention=r["would_intervention"],
                reason_code=r["reason_code"],
                r_milli=r["r_milli"],
                clause=r["clause"],
                explain_refs=json.loads(r["explain_refs"]),
                state_as_of=datetime.fromisoformat(r["state_as_of"]) if r["state_as_of"] else None,
                observed_at=datetime.fromisoformat(r["observed_at"]),
            )
            for r in rows
        ]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM bypass_observation").fetchone()["c"])

    def close(self) -> None:
        self._conn.close()
