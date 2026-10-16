#!/usr/bin/env python3
"""Generate REAL input/output examples by running the full kernel chain.

Outputs a single JSON document with:
  - ingest request/response (event envelopes)
  - RiskOutput (pure function)
  - executor verdict (bypass observe mode)
  - explain payload (disclosure)
  - projection artifacts (four_color / doc6)

Everything below is produced by the actual code paths — no hand-written JSON.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
for area in ("event-log", "risk-function", "executor", "disclosure", "projection", "verification"):
    p = ROOT / "areas" / area
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from eventlog import EventEnvelope, SqliteStore  # noqa: E402
from eventlog.projector import SqliteProjector  # noqa: E402
from executor import ExecutorService  # noqa: E402
from disclosure import ExplainService  # noqa: E402
from projection import ProjectionService  # noqa: E402
from riskfn import load_policy  # noqa: E402

AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
EARLY = AS_OF - timedelta(days=2)
NOW = AS_OF + timedelta(minutes=1)


def eid(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def envelope(
    *,
    object_id: str,
    object_type: str,
    type_: str,
    payload: dict,
    occurred_at: datetime,
    source_system: str,
    operator: str | None = None,
    trace_id: str,
    n: int,
    ingested_at: datetime | None = None,
) -> dict:
    ev = EventEnvelope(
        event_id=eid(n),
        tenant_id="t1",
        trace_id=trace_id,
        object_id=object_id,
        object_type=object_type,
        occurred_at=occurred_at,
        ingested_at=ingested_at or occurred_at + timedelta(seconds=1),
        type=type_,
        payload=payload,
        provenance={"source_system": source_system, "operator": operator},
    )
    d = ev.model_dump(mode="json")
    d["event_id"] = str(ev.event_id)
    return d


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(Path(tmp) / "demo.db")
        policy = load_policy("v1")
        projector = SqliteProjector(store, policy, "t1")
        executor = ExecutorService(projector, policy, state_db_path=str(Path(tmp) / "executor.db"))
        explain_service = ExplainService(store, projector, executor, policy, "t1")
        projection_service = ProjectionService(
            store, projector, executor, policy, "t1", artifacts_dir=Path(tmp) / "artifacts"
        )

        out: dict = {"as_of": AS_OF.isoformat(), "tenant_id": "t1"}

        # ---------- 1. ingest 输入（三个事件的信封原文） ----------
        ev_telemetry = envelope(
            object_id="veh-demo-001", object_type="vehicle", type_="telemetry",
            occurred_at=EARLY, source_system="obd-gateway", trace_id="tr-demo-1", n=101,
            payload={
                "window_start": EARLY.isoformat(),
                "window_end": (EARLY + timedelta(minutes=10)).isoformat(),
                "speed_max_kmh": 128,
                "continuous_driving_minutes": 310,
                "device_id": "obd-9f2c",
            },
        )
        ev_violation = envelope(
            object_id="veh-demo-001", object_type="vehicle", type_="violation",
            occurred_at=EARLY + timedelta(hours=1), source_system="enforcement-platform",
            trace_id="tr-demo-2", n=102,
            payload={
                "violation_kind": "speeding",
                "count": 4,
                "duration_minutes": 40,
                "detected_at": (EARLY + timedelta(hours=1)).isoformat(),
            },
        )
        ev_registry = envelope(
            object_id="veh-demo-001", object_type="vehicle", type_="registry",
            occurred_at=EARLY + timedelta(hours=2), source_system="vehicle-registry",
            trace_id="tr-demo-3", n=103,
            payload={
                "registry_kind": "tank_inspection",
                "expires_on": "2026-10-01",
                "status": "expired",
                "document_ref": "TJ-2025-114",
            },
        )

        out["1_ingest_requests"] = [ev_telemetry, ev_violation, ev_registry]
        out["1_ingest_responses"] = [
            {
                "event_id": r.event_id,
                "seq": r.seq,
                "duplicate": r.duplicate,
            }
            for r in (
                store.append(EventEnvelope.model_validate(ev_telemetry)),
                store.append(EventEnvelope.model_validate(ev_violation)),
                store.append(EventEnvelope.model_validate(ev_registry)),
            )
        ]

        # ---------- 2. 纯函数输出 ----------
        projection = projector.project_object("veh-demo-001", AS_OF)
        out["2_risk_output"] = json.loads(projection.decision.canonical_json())

        # ---------- 3. 执行器 verdict（旁路观察期） ----------
        verdict = executor.evaluate("veh-demo-001", "allow_dispatch", NOW)
        out["3_executor_verdict"] = json.loads(verdict.model_dump_json())

        # ---------- 4. explain（披露层） ----------
        out["4_explain"] = json.loads(
            explain_service.explain("veh-demo-001").model_dump_json()
        )

        # ---------- 5. 覆写示例 ----------
        ev_override = envelope(
            object_id="veh-demo-001", object_type="vehicle", type_="manual_override",
            occurred_at=NOW, source_system="dispatch-desk", operator="dispatcher-02",
            trace_id="tr-demo-4", n=104,
            payload={
                "override_level": "clear",
                "reason": "客户紧急提货，安全部门已审批",
                "approver": "safety-lead-1",
                "expires_at": (NOW + timedelta(hours=72)).isoformat(),
            },
        )
        store.append(EventEnvelope.model_validate(ev_override))
        projector.project_object("veh-demo-001", NOW)
        verdict_ov = executor.evaluate("veh-demo-001", "allow_dispatch", NOW)
        out["5_override_ingest_request"] = ev_override
        out["5_verdict_under_override"] = json.loads(verdict_ov.model_dump_json())

        # ---------- 6. 监管投影 ----------
        out["6_projection_four_color"] = projection_service.generate_fourcolor(AS_OF, NOW)
        out["6_projection_doc6"] = projection_service.generate_doc6(AS_OF, NOW)

        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
