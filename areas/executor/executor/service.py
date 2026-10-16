"""ExecutorService — the dispatch-hook boundary (W-006).

Wiring (single deployable, D-005):
- reads the latest RiskOutput + override state through the PROJECTOR's read
  interface (never the log, never the function — executor rule 1);
- owns its hysteresis state + bypass observations in its own store;
- exposes evaluate() for the dispatch flow and a FastAPI router for ops.

Bypass mode (W-006): every predicate ships mode="observe" — would-deny requests
are allowed through and recorded in bypass_observation for the W-009 dual-run.
W-010 flips allow_dispatch to mode="enforce" behind a rollout switch.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from .observation import BypassObservation, ObservationStore, SqliteObservationStore
from .predicates import (
    HysteresisState,
    Predicate,
    PredicateEngine,
    Thresholds,
    Verdict,
    load_predicates,
)


class EvaluateRequest(BaseModel):
    """POST /evaluate body (module-level: FastAPI resolves annotations against
    module globals; a closure-local model is invisible under
    `from __future__ import annotations`)."""

    object_id: str
    predicate: str = "allow_dispatch"


class DecisionReader(Protocol):
    def read_decision(self, object_id: str): ...


class OverrideReader(Protocol):
    def read_override(self, object_id: str): ...


class StateStore(Protocol):
    def load_state(self, object_id: str, predicate: str) -> HysteresisState | None: ...
    def save_state(self, object_id: str, predicate: str, state: HysteresisState, now: datetime) -> None: ...


class SqliteStateStore:
    """Hysteresis state over the executor DDL."""

    def __init__(self, db_path: str) -> None:
        import sqlite3
        from pathlib import Path

        ddl = Path(__file__).resolve().parent.parent / "ddl" / "sqlite" / "001_executor.sql"
        self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(ddl.read_text(encoding="utf-8"))

    def load_state(self, object_id: str, predicate: str) -> HysteresisState | None:
        row = self._conn.execute(
            "SELECT * FROM intervention_state WHERE tenant_id='t1' AND object_id=? AND predicate=?",
            (object_id, predicate),
        ).fetchone()
        if row is None:
            return None
        from datetime import datetime as _dt

        return HysteresisState(
            intervention=row["intervention"],
            r_at_entry=row["r_at_entry"],
            since=_dt.fromisoformat(row["since"]) if row["since"] else None,
        )

    def save_state(self, object_id: str, predicate: str, state: HysteresisState, now: datetime) -> None:
        self._conn.execute(
            """
            INSERT INTO intervention_state (tenant_id, object_id, predicate, intervention, r_at_entry, since, updated_at)
            VALUES ('t1',?,?,?,?,?,?)
            ON CONFLICT(tenant_id, object_id, predicate) DO UPDATE SET
                intervention=excluded.intervention, r_at_entry=excluded.r_at_entry,
                since=excluded.since, updated_at=excluded.updated_at
            """,
            (
                object_id,
                predicate,
                state.intervention,
                state.r_at_entry,
                state.since.isoformat() if state.since else None,
                now.isoformat(),
            ),
        )


class ExecutorService:
    def __init__(
        self,
        projector,
        policy,
        *,
        state_db_path: str,
        observation_store: ObservationStore | None = None,
        predicates: dict[str, Predicate] | None = None,
        predicate_mode: str | None = None,
    ) -> None:
        self.projector = projector
        self.engine = PredicateEngine(
            Thresholds(
                alert=policy.executor.thresholds["alert"],
                restrict=policy.executor.thresholds["restrict"],
                lock=policy.executor.thresholds["lock"],
                hysteresis_milli=policy.executor.hysteresis_milli,
            )
        )
        loaded = predicates or load_predicates()
        # 运维切流开关（RISKCORE_PREDICATE_MODE）：observe=旁路观察，enforce=真拦截。
        # 只允许整体切换，不允许单谓词绕过——切流是受控动作（W-010）。
        if predicate_mode is not None:
            if predicate_mode not in ("observe", "enforce"):
                raise ValueError(f"invalid RISKCORE_PREDICATE_MODE: {predicate_mode}")
            loaded = {name: p.model_copy(update={"mode": predicate_mode}) for name, p in loaded.items()}
        self.predicates = loaded
        self.states = SqliteStateStore(state_db_path)
        self.observations = observation_store or SqliteObservationStore(state_db_path)

    # ------------------------------------------------------------------ core

    def evaluate(self, object_id: str, predicate_name: str, now: datetime) -> Verdict:
        predicate = self.predicates[predicate_name]
        decision = self.projector.read_decision(object_id)
        override = self.projector.read_override(object_id)
        state = self.states.load_state(object_id, predicate_name)

        verdict = self.engine.evaluate(predicate, decision, override, state, now, object_id=object_id)

        # 滞后状态推进（只记"是否干预"，不回写 r — 区域规则 2）
        entered = verdict.intervention != "none" and (state is None or state.intervention == "none")
        released = verdict.intervention == "none" and state is not None and state.intervention != "none"
        if entered:
            new_state = HysteresisState(
                intervention=verdict.intervention,
                r_at_entry=verdict.r_milli,
                since=now,
            )
        elif released:
            new_state = HysteresisState(intervention="none", r_at_entry=None, since=None)
        elif state is not None and verdict.intervention == state.intervention:
            new_state = state  # 带内维持
        else:
            new_state = HysteresisState(
                intervention=verdict.intervention, r_at_entry=verdict.r_milli, since=now
            )
        self.states.save_state(object_id, predicate_name, new_state, now)

        # 旁路观察：会拦截的请求留痕（W-006 → W-009 双跑语料）
        if not verdict.enforced and verdict.intervention == "lock":
            self.observations.record(
                BypassObservation(
                    object_id=object_id,
                    predicate=predicate_name,
                    would_intervention=verdict.intervention,
                    reason_code=verdict.reason_code,
                    r_milli=verdict.r_milli,
                    clause=verdict.clause,
                    explain_refs=verdict.explain_refs,
                    state_as_of=verdict.state_as_of,
                    observed_at=now,
                )
            )
        return verdict

    # ------------------------------------------------------------------ read

    def active_interventions(self, object_id: str) -> list[dict]:
        """Read-only view of current interventions (for the disclosure layer)."""
        rows = self.states._conn.execute(
            "SELECT * FROM intervention_state WHERE tenant_id='t1' AND object_id=? AND intervention != 'none'",
            (object_id,),
        ).fetchall()
        out = []
        for row in rows:
            pred = self.predicates.get(row["predicate"])
            bound = pred.bound if pred else "lock"
            enter = getattr(self.engine.thresholds, bound)
            exit_bound = enter - self.engine.thresholds.hysteresis_milli
            release = f"r < {exit_bound}"
            if row["predicate"] in ("lock_vehicle", "allow_dispatch"):
                release += " 且相关整改工单闭环"
            out.append(
                {
                    "predicate": row["predicate"],
                    "intervention": row["intervention"],
                    "since": row["since"],
                    "clause": pred.clause if pred else "",
                    "release_condition": release,
                    "reason_hint": "强制条款命中" if bound == "lock" else "风险值越限",
                }
            )
        return out

    # ------------------------------------------------------------------- api

    def build_router(self):
        from fastapi import APIRouter

        service = self

        router = APIRouter(tags=["executor"])

        @router.post("/evaluate")
        def evaluate(req: EvaluateRequest) -> dict:
            from datetime import datetime, timezone

            verdict = service.evaluate(req.object_id, req.predicate, datetime.now(timezone.utc))
            return verdict.model_dump(mode="json")

        @router.get("/observations")
        def observations(limit: int = 50) -> dict:
            return {"observations": [o.__dict__ for o in service.observations.list(limit=limit)]}

        return router
