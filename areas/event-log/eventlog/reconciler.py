"""Reconciler — 铁律三的工程实现 (projector.md §对账守护).

Every N minutes (default 10): sample objects, FULL-recompute from the log,
compare against the caches. Daily: full-object sweep. Any diff ≠ 0 →
- alert (drift report),
- fix the cache from the recompute (重算为准),
- record the drift pattern,
- append a projection_only reconciliation record to the log (留痕).

Selection is deterministic (sorted ids + seeded sample) so replays of the
reconciliation itself are reproducible.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from riskfn import PolicyDocument

from .envelope import EventEnvelope, Provenance
from .store import SqliteStore
from .types import EventType, ObjectType


@dataclass
class DriftReport:
    object_id: str
    mode: str                       # sample | full
    fact_diffs: list[tuple[str, str, str]] = field(default_factory=list)  # (fact, cached, recomputed)
    r_cached: int | None = None
    r_recomputed: int | None = None
    drifted: bool = False
    fixed: bool = False

    def summary(self) -> str:
        if not self.drifted:
            return f"{self.object_id}: no drift"
        return (
            f"{self.object_id}: DRIFT facts={self.fact_diffs} "
            f"r {self.r_cached}->{self.r_recomputed} fixed={self.fixed}"
        )


class Reconciler:
    def __init__(self, store: SqliteStore, projector, tenant_id: str) -> None:
        self.store = store
        self.projector = projector
        self.tenant = tenant_id

    # -------------------------------------------------------------- compare

    def reconcile_object(self, object_id: str, as_of: datetime, mode: str = "sample") -> DriftReport:
        # 1. 先读旧缓存（在任何重写之前——否则漂移会被自己的修复抹掉）
        cached_snapshot = self.projector.read_snapshot(object_id)
        cached_decision = self.projector.read_decision(object_id)

        # 2. 从日志全量重算（重算为准）
        recomputed = self.projector.project_object(object_id, as_of)

        report = DriftReport(object_id=object_id, mode=mode)
        if cached_decision is not None:
            report.r_cached = cached_decision.r_milli
        report.r_recomputed = recomputed.decision.r_milli

        fact_diffs: list[tuple[str, str, str]] = []
        if cached_snapshot is None:
            fact_diffs.append(("<all>", "MISSING", "present"))
        else:
            for name in sorted(recomputed.snapshot.facts):
                fresh = recomputed.snapshot.facts[name]
                old = cached_snapshot.facts.get(name)
                if old is None:
                    fact_diffs.append((name, "MISSING", "present"))
                    continue
                if old.freshness != fresh.freshness:
                    fact_diffs.append((name, f"freshness={old.freshness}", f"freshness={fresh.freshness}"))
                if sorted(c.event_id for c in old.contributions) != sorted(
                    c.event_id for c in fresh.contributions
                ):
                    fact_diffs.append((name, "contributions differ", "contributions differ"))
                if (old.days_remaining or 0) != (fresh.days_remaining or 0):
                    fact_diffs.append(
                        (name, f"days={old.days_remaining}", f"days={fresh.days_remaining}")
                    )
        report.fact_diffs = fact_diffs

        r_drift = cached_decision is None or cached_decision.r_milli != recomputed.decision.r_milli
        if fact_diffs or r_drift:
            report.drifted = True
            # 以重算为准修正缓存（投影器刚写过最新值；此处显式重写确保修复语义）
            self.projector.project_object(object_id, as_of)
            report.fixed = True
            self._record(as_of, object_id, report)
        return report

    def reconcile_sample(self, as_of: datetime, n: int = 20, seed: int = 20261015) -> list[DriftReport]:
        ids = sorted(self.store.iter_objects(self.tenant))
        if not ids:
            return []
        rng = random.Random(seed)
        chosen = sorted(rng.sample(ids, min(n, len(ids))))
        return [self.reconcile_object(oid, as_of, mode="sample") for oid in chosen]

    def reconcile_full(self, as_of: datetime) -> list[DriftReport]:
        return [
            self.reconcile_object(oid, as_of, mode="full")
            for oid in sorted(self.store.iter_objects(self.tenant))
        ]

    # -------------------------------------------------------------- record

    def _record(self, as_of: datetime, object_id: str, report: DriftReport) -> None:
        object_type = self.projector.object_type_of(object_id) or "vehicle"
        ev = EventEnvelope(
            event_id=uuid4(),
            tenant_id=self.tenant,
            trace_id=f"reconcile-{object_id}",
            object_id=object_id,
            object_type=ObjectType(object_type),
            occurred_at=as_of,
            ingested_at=as_of,
            type=EventType.PROJECTION_ONLY,
            payload={
                "material_kind": "reconciliation_run",
                "data": {
                    "mode": report.mode,
                    "drifted": report.drifted,
                    "fixed": report.fixed,
                    "fact_diffs": [list(d) for d in report.fact_diffs],
                    "r_cached": report.r_cached,
                    "r_recomputed": report.r_recomputed,
                },
            },
            provenance=Provenance(source_system="reconciler"),
        )
        self.store.append(ev)
