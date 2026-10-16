"""State projector — log → fact snapshots + decision cache + watermark (W-005).

Contract (projector.md + D-001/D-002):
- the log is the ontology; these tables are disposable caches;
- the projector is the ONLY writer of fact_snapshot / decision_cache /
  projection_state; other areas read through read_snapshot()/read_decision();
- NO wall clock inside the projector: as_of is always injected
  (AGENTS.md iron law 1 — the projector body obeys it too);
- every run recomputes the object from its full event stream (idempotent,
  replay-safe). Incremental maintenance is a future optimization; the
  reconciler is the safety net until then;
- correction chains are applied BEFORE fact derivation: the effective payload
  of a corrected event is its latest correction's payload (the original event
  and its provenance stay untouched in the log);
- projection_only events never touch facts (area rule 5) — reconciliation
  records are themselves projection_only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from riskfn import (
    FACT_NAMES,
    FactContribution,
    FactEntry,
    PolicyDocument,
    RiskContext,
    RiskOutput,
    StateSnapshot,
    risk,
)

from .corrections import apply_corrections
from .envelope import EventEnvelope, StoredEvent, parse_payload
from .store import SqliteStore
from .types import EventType, ObjectType

PROJECTION_DDL = Path(__file__).resolve().parent.parent / "ddl" / "sqlite" / "002_projection.sql"

#: carrier → policy decay half-life key (freshness window = one half-life)
_CARRIER_HALF_LIFE_KEY = {
    "violation": "violation",
    "telemetry": "telemetry",
    "inspection": "inspection",
    "registry": "registry",
    "work_order": "work_order",
    "expert_assertion": "expert_assertion",
}


def _age_days(occurred_at: datetime, as_of: datetime) -> int:
    delta = as_of - occurred_at
    return max(0, delta.days)


@dataclass
class ObjectProjection:
    object_id: str
    object_type: str
    watermark_seq: int
    snapshot: StateSnapshot
    decision: RiskOutput
    override: "OverrideState | None" = None


@dataclass
class OverrideState:
    """Active manual override (D-009) — the executor's top-priority input."""

    level: str          # alert | restrict | lock | clear
    operator: str
    approver: str
    reason: str
    expires_at: datetime
    event_id: str


class Projector(Protocol):
    def project_object(self, object_id: str, as_of: datetime) -> ObjectProjection: ...
    def project_all(self, as_of: datetime) -> list[ObjectProjection]: ...
    def rebuild_all(self, as_of: datetime) -> None: ...
    def read_snapshot(self, object_id: str) -> StateSnapshot | None: ...
    def read_decision(self, object_id: str) -> RiskOutput | None: ...
    def object_type_of(self, object_id: str) -> str | None: ...


class SqliteProjector:
    """Projector over the SQLite dev store (same interface as the PG one)."""

    def __init__(self, store: SqliteStore, policy: PolicyDocument, tenant_id: str) -> None:
        self.store = store
        self.policy = policy
        self.tenant = tenant_id
        self._conn = sqlite3.connect(store.db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(PROJECTION_DDL.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ derive

    def _effective_events(self, events: list[StoredEvent]) -> list[StoredEvent]:
        """Apply correction chains (shared resolver — see corrections.py).."""
        return apply_corrections(events)

    def _half_life(self, carrier: str) -> int:
        hl = self.policy.scoring.decay["half_life_days"]
        key = _CARRIER_HALF_LIFE_KEY.get(carrier, carrier)
        if key not in hl:
            raise ValueError(f"policy has no half-life for carrier '{carrier}'")
        return hl[key]

    def _derive(
        self, events: list[StoredEvent], as_of: datetime
    ) -> tuple[dict[str, FactEntry], list[str]]:
        thresholds = self.policy.scoring.sensor_thresholds
        contribs: dict[str, list[FactContribution]] = {name: [] for name in FACT_NAMES}
        veto: set[str] = set()

        # pre-pass: closed work orders (closure pairing key)
        closed_refs: set[str] = set()
        for ev in events:
            if ev.type is EventType.WORK_ORDER_CLOSED:
                closed_refs.add(str(ev.payload["closes_order_id"]))

        registry_latest: dict[str, StoredEvent] = {}
        for ev in events:
            if ev.type is not EventType.REGISTRY:
                continue
            kind = str(ev.payload["registry_kind"])
            cur = registry_latest.get(kind)
            if cur is None or ev.seq > cur.seq:
                registry_latest[kind] = ev

        for ev in events:
            if ev.occurred_at > as_of:
                continue  # not yet observable at as_of
            age = _age_days(ev.occurred_at, as_of)
            eid = str(ev.event_id)

            if ev.type is EventType.VIOLATION:
                kind = str(ev.payload["violation_kind"])
                fact = {
                    "speeding": "violation_speeding",
                    "fatigue": "violation_fatigue",
                    "accident": "violation_accident",
                }.get(kind)
                if fact is None:
                    continue
                contribs[fact].append(
                    FactContribution(event_id=eid, value=int(ev.payload["count"]), age_days=age)
                )
                if fact == "violation_accident":
                    veto.add("accident")

            elif ev.type is EventType.TELEMETRY:
                if int(ev.payload["continuous_driving_minutes"]) > thresholds["continuous_driving_minutes"]:
                    contribs["telemetry_fatigue_windows"].append(
                        FactContribution(event_id=eid, value=1, age_days=age)
                    )
                if int(ev.payload["speed_max_kmh"]) > thresholds["speed_max_kmh"]:
                    contribs["telemetry_speeding_windows"].append(
                        FactContribution(event_id=eid, value=1, age_days=age)
                    )

            elif ev.type is EventType.INSPECTION:
                level = str(ev.payload["hazard_level"])
                ref = ev.payload.get("hazard_ref")
                if ref is not None and str(ref) in closed_refs:
                    continue  # closed hazard — no longer open
                if level == "major":
                    contribs["open_hazard_major"].append(
                        FactContribution(event_id=eid, value=1, age_days=age)
                    )
                    veto.add("major_hazard_open")
                elif level == "general":
                    contribs["open_hazard_general"].append(
                        FactContribution(event_id=eid, value=1, age_days=age)
                    )

            elif ev.type is EventType.WORK_ORDER_CREATED:
                ref = str(ev.payload["hazard_ref"])
                if ref in closed_refs:
                    continue
                due = ev.payload["due_date"]
                due_dt = datetime.fromisoformat(str(due)).replace(tzinfo=timezone.utc)
                if due_dt < as_of:
                    contribs["open_work_order_overdue"].append(
                        FactContribution(event_id=eid, value=1, age_days=age)
                    )

            elif ev.type is EventType.EXPERT_ASSERTION:
                valid_until = datetime.fromisoformat(str(ev.payload["valid_until"])).replace(
                    tzinfo=timezone.utc
                )
                if valid_until >= as_of:
                    payload = parse_payload(EventType.EXPERT_ASSERTION, ev.payload)
                    contribs["lecd_prior"].append(
                        FactContribution(event_id=eid, value=payload.d_score, age_days=age)
                    )

        # registry facts + vetoes (latest event per kind wins)
        facts: dict[str, FactEntry] = {}
        days_by_kind = {"license": "license_days_remaining", "tank_inspection": "tank_inspection_days_remaining"}
        for kind, ev in registry_latest.items():
            fact_name = days_by_kind.get(kind)
            if fact_name is None:
                continue
            if ev.occurred_at > as_of:
                continue
            age = _age_days(ev.occurred_at, as_of)
            expires = datetime.fromisoformat(str(ev.payload["expires_on"])).replace(tzinfo=timezone.utc)
            days_remaining = (expires - as_of).days
            status = str(ev.payload["status"])
            contribs[fact_name].append(
                FactContribution(event_id=str(ev.event_id), value=0, age_days=age)
            )
            if status != "valid" or days_remaining <= 0:
                veto.add("license_invalid" if kind == "license" else "tank_inspection_overdue")
            facts[fact_name] = FactEntry(
                name=fact_name,
                freshness="valid",
                contributions=contribs[fact_name],
                days_remaining=days_remaining,
                registry_status=status,
            )

        # accident veto only while inside the violation window (recent accident)
        hl_violation = self._half_life("violation")
        if "accident" in veto:
            recent = [c for c in contribs["violation_accident"] if c.age_days <= hl_violation]
            if not recent:
                veto.discard("accident")
            else:
                contribs["violation_accident"] = recent

        # assemble every tracked fact (missing ones included, for coverage)
        for name in FACT_NAMES:
            if name in facts:
                continue
            cs = contribs[name]
            carrier = {
                "violation_speeding": "violation",
                "violation_fatigue": "violation",
                "violation_accident": "violation",
                "telemetry_fatigue_windows": "telemetry",
                "telemetry_speeding_windows": "telemetry",
                "open_hazard_major": "inspection",
                "open_hazard_general": "inspection",
                "open_work_order_overdue": "work_order",
                "lecd_prior": "expert_assertion",
            }.get(name, "violation")
            if name == "lecd_prior":
                # freshness is assertion validity, already enforced above
                freshness = "valid" if cs else "missing"
                entry = FactEntry(
                    name=name,
                    freshness=freshness,
                    contributions=cs,
                    # 先验取最高危险源（max D）主导：保守且与"辨识清单"语义一致
                    prior_r_milli=max((c.value for c in cs), default=None),
                )
            else:
                if not cs:
                    freshness = "missing"
                else:
                    hl = self._half_life(carrier)
                    freshness = "valid" if max(c.age_days for c in cs) <= hl else "stale"
                entry = FactEntry(name=name, freshness=freshness, contributions=cs)
            facts[name] = entry

        return facts, sorted(veto)

    def _derive_override(self, events: list[StoredEvent], as_of: datetime) -> OverrideState | None:
        """Latest manual_override by seq; active iff expires_at > as_of (D-009)."""
        latest: StoredEvent | None = None
        for ev in events:
            if ev.type is EventType.MANUAL_OVERRIDE:
                if latest is None or ev.seq > latest.seq:
                    latest = ev
        if latest is None:
            return None
        expires_at = datetime.fromisoformat(str(latest.payload["expires_at"]))
        if expires_at <= as_of:
            return None  # 到期自动失效，不自动续期
        return OverrideState(
            level=str(latest.payload["override_level"]),
            operator=str(latest.provenance.operator or ""),
            approver=str(latest.payload["approver"]),
            reason=str(latest.payload["reason"]),
            expires_at=expires_at,
            event_id=str(latest.event_id),
        )

    # ----------------------------------------------------------------- project

    def project_object(self, object_id: str, as_of: datetime) -> ObjectProjection:
        events = self.store.read_object_events(self.tenant, object_id)
        if not events:
            raise KeyError(f"no events for object {object_id}")
        object_type = events[0].object_type.value
        effective = self._effective_events(events)
        facts, veto_flags = self._derive(effective, as_of)
        override = self._derive_override(effective, as_of)
        watermark_seq = max(e.seq for e in events)

        snapshot = StateSnapshot.build(
            object_id=object_id,
            as_of=as_of,
            facts=facts,
            veto_flags=veto_flags,
            watermark_seq=watermark_seq,
        )
        decision = risk(
            snapshot,
            self.policy,
            RiskContext(
                object_id=object_id,
                as_of=as_of,
                policy_id=self.policy.policy_id,
                watermark=watermark_seq,
            ),
        )

        self._write_snapshot(object_id, facts, watermark_seq, as_of)
        self._write_decision(object_id, decision, watermark_seq, as_of)
        self._write_override(object_id, override, as_of)
        self._upsert_state(object_id, object_type, watermark_seq)
        self.store.advance_watermark(self.tenant, object_id, watermark_seq, as_of)

        return ObjectProjection(
            object_id=object_id,
            object_type=object_type,
            watermark_seq=watermark_seq,
            snapshot=snapshot,
            decision=decision,
            override=override,
        )

    def project_all(self, as_of: datetime) -> list[ObjectProjection]:
        return [self.project_object(oid, as_of) for oid in self.store.iter_objects(self.tenant)]

    def rebuild_all(self, as_of: datetime) -> None:
        """Cache rebuild SOP: drop projection tables, replay the whole log."""
        self._conn.execute("DELETE FROM fact_snapshot WHERE tenant_id=?", (self.tenant,))
        self._conn.execute("DELETE FROM decision_cache WHERE tenant_id=?", (self.tenant,))
        self._conn.execute("DELETE FROM override_state WHERE tenant_id=?", (self.tenant,))
        self._conn.execute("DELETE FROM projection_state WHERE tenant_id=?", (self.tenant,))
        self.project_all(as_of)

    # ------------------------------------------------------------------- write

    def _write_snapshot(
        self, object_id: str, facts: dict[str, FactEntry], watermark_seq: int, as_of: datetime
    ) -> None:
        for name, entry in facts.items():
            self._conn.execute(
                """
                INSERT INTO fact_snapshot (
                    tenant_id, object_id, fact_name, freshness, contributions,
                    days_remaining, registry_status, prior_r_milli, last_event_seq, updated_as_of
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id, object_id, fact_name) DO UPDATE SET
                    freshness=excluded.freshness,
                    contributions=excluded.contributions,
                    days_remaining=excluded.days_remaining,
                    registry_status=excluded.registry_status,
                    prior_r_milli=excluded.prior_r_milli,
                    last_event_seq=excluded.last_event_seq,
                    updated_as_of=excluded.updated_as_of
                """,
                (
                    self.tenant,
                    object_id,
                    name,
                    entry.freshness,
                    json.dumps(
                        [c.model_dump(mode="json") for c in entry.contributions],
                        sort_keys=True,
                    ),
                    entry.days_remaining,
                    entry.registry_status,
                    entry.prior_r_milli,
                    watermark_seq,
                    as_of.isoformat(),
                ),
            )

    def _write_decision(
        self, object_id: str, decision: RiskOutput, watermark_seq: int, as_of: datetime
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO decision_cache (
                tenant_id, object_id, policy_id, r_milli, veto_active, veto_names,
                confidence, explain_refs, as_of, seq_upto, computed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id, object_id, policy_id) DO UPDATE SET
                r_milli=excluded.r_milli,
                veto_active=excluded.veto_active,
                veto_names=excluded.veto_names,
                confidence=excluded.confidence,
                explain_refs=excluded.explain_refs,
                as_of=excluded.as_of,
                seq_upto=excluded.seq_upto,
                computed_at=excluded.computed_at
            """,
            (
                self.tenant,
                object_id,
                decision.policy_id,
                decision.r_milli,
                1 if decision.veto_active else 0,
                json.dumps(sorted(decision.veto_names)),
                json.dumps(decision.confidence.model_dump(mode="json"), sort_keys=True),
                json.dumps([e.model_dump(mode="json") for e in decision.explain], sort_keys=True),
                as_of.isoformat(),
                watermark_seq,
                as_of.isoformat(),
            ),
        )

    def _write_override(self, object_id: str, override: OverrideState | None, as_of: datetime) -> None:
        if override is None:
            self._conn.execute(
                "DELETE FROM override_state WHERE tenant_id=? AND object_id=?",
                (self.tenant, object_id),
            )
            return
        self._conn.execute(
            """
            INSERT INTO override_state (
                tenant_id, object_id, level, operator, approver, reason, expires_at, event_id, as_of
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id, object_id) DO UPDATE SET
                level=excluded.level, operator=excluded.operator, approver=excluded.approver,
                reason=excluded.reason, expires_at=excluded.expires_at,
                event_id=excluded.event_id, as_of=excluded.as_of
            """,
            (
                self.tenant,
                object_id,
                override.level,
                override.operator,
                override.approver,
                override.reason,
                override.expires_at.isoformat(),
                override.event_id,
                as_of.isoformat(),
            ),
        )

    def read_override(self, object_id: str) -> OverrideState | None:
        row = self._conn.execute(
            "SELECT * FROM override_state WHERE tenant_id=? AND object_id=?",
            (self.tenant, object_id),
        ).fetchone()
        if row is None:
            return None
        return OverrideState(
            level=row["level"],
            operator=row["operator"],
            approver=row["approver"],
            reason=row["reason"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
            event_id=row["event_id"],
        )

    def _upsert_state(self, object_id: str, object_type: str, watermark_seq: int) -> None:
        self._conn.execute(
            """
            INSERT INTO projection_state (tenant_id, object_id, object_type, last_seq, watermark)
            VALUES (?,?,?,?,?)
            ON CONFLICT(tenant_id, object_id) DO UPDATE SET
                last_seq=excluded.last_seq, watermark=excluded.watermark
            """,
            (self.tenant, object_id, object_type, watermark_seq, watermark_seq),
        )

    # -------------------------------------------------------------------- read

    def object_type_of(self, object_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT object_type FROM projection_state WHERE tenant_id=? AND object_id=?",
            (self.tenant, object_id),
        ).fetchone()
        return row["object_type"] if row else None

    def read_snapshot(self, object_id: str) -> StateSnapshot | None:
        rows = self._conn.execute(
            "SELECT * FROM fact_snapshot WHERE tenant_id=? AND object_id=? ORDER BY fact_name",
            (self.tenant, object_id),
        ).fetchall()
        if not rows:
            return None
        wm_row = self._conn.execute(
            "SELECT watermark FROM projection_state WHERE tenant_id=? AND object_id=?",
            (self.tenant, object_id),
        ).fetchone()
        facts: dict[str, FactEntry] = {}
        for r in rows:
            facts[r["fact_name"]] = FactEntry(
                name=r["fact_name"],
                freshness=r["freshness"],
                contributions=[FactContribution.model_validate(c) for c in json.loads(r["contributions"])],
                days_remaining=r["days_remaining"],
                registry_status=r["registry_status"],
                prior_r_milli=r["prior_r_milli"],
            )
        as_of_row = self._conn.execute(
            "SELECT as_of FROM decision_cache WHERE tenant_id=? AND object_id=? AND policy_id=?",
            (self.tenant, object_id, self.policy.policy_id),
        ).fetchone()
        as_of = datetime.fromisoformat(as_of_row["as_of"]) if as_of_row else datetime.now(timezone.utc)
        return StateSnapshot.build(
            object_id=object_id,
            as_of=as_of,
            facts=facts,
            watermark_seq=int(wm_row["watermark"]) if wm_row else 0,
        )

    def read_decision(self, object_id: str) -> RiskOutput | None:
        row = self._conn.execute(
            "SELECT * FROM decision_cache WHERE tenant_id=? AND object_id=? AND policy_id=?",
            (self.tenant, object_id, self.policy.policy_id),
        ).fetchone()
        if row is None:
            return None
        return RiskOutput(
            r_milli=row["r_milli"],
            veto_active=bool(row["veto_active"]),
            veto_names=json.loads(row["veto_names"]) if "veto_names" in row.keys() else [],
            confidence=json.loads(row["confidence"]),
            explain=json.loads(row["explain_refs"]),
            policy_id=row["policy_id"],
            as_of=datetime.fromisoformat(row["as_of"]),
            object_id=object_id,
            watermark_seq=row["seq_upto"],
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteProjector":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
