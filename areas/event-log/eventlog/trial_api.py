"""Trial API — dispatcher usability trial records (W-007 acceptance).

A trial evaluates ONE object: can a dispatcher produce the full evidence chain
within 30 seconds? The record is appended as a projection_only event
(material_kind=trial_record) — no new tables; the trial ledger is rebuilt from
the log, same pattern as reconciliation records. Write path stays in the
event-log area (the only writer).
"""
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .envelope import EventEnvelope, Provenance
from .types import EventType, ObjectType

TENANT_HEADER = "X-Tenant-Id"

#: 分项评分键（与试用评估页一致）
SCORE_KEYS = ("conclusion", "drilldown", "confidence", "intervention", "override", "overall")
PASS_AVG_MILLI = 4000  # 平均分 ≥ 4.0
TIME_LIMIT_SECONDS = 30


class TrialSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluator: str = Field(min_length=1)
    role: str = "dispatcher"
    object_id: str = Field(min_length=1)
    elapsed_seconds: int = Field(ge=0)
    scores: dict[str, int] = Field(description="各项 1-5 分；override 不适用可缺省")
    would_rely: str = Field(pattern=r"^(yes|no|unsure)$")
    comments: str = ""
    started_at: datetime


def evaluate_submission(sub: TrialSubmission) -> dict:
    present = {k: v for k, v in sub.scores.items() if k in SCORE_KEYS}
    for k, v in present.items():
        if not 1 <= v <= 5:
            raise HTTPException(status_code=422, detail=f"score '{k}' must be 1..5")
    required = [k for k in SCORE_KEYS if k != "override"]
    missing = [k for k in required if k not in present]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing scores: {missing}")
    avg_milli = (sum(present.values()) * 1000) // len(present)
    within_30s = sub.elapsed_seconds <= TIME_LIMIT_SECONDS
    passed = within_30s and avg_milli >= PASS_AVG_MILLI
    return {
        "average_score_milli": avg_milli,
        "within_30s": within_30s,
        "passed": passed,
        "recorded_scores": present,
    }


def build_trial_router() -> APIRouter:
    router = APIRouter(tags=["trial"])

    def _get_store():
        from .app import get_ingest_state

        return get_ingest_state().store

    def require_tenant(x_tenant_id: Annotated[str | None, Header()] = None) -> str:
        if not x_tenant_id:
            raise HTTPException(status_code=401, detail="missing tenant: X-Tenant-Id header required")
        return x_tenant_id

    @router.post("/trial/submit")
    def submit(sub: TrialSubmission, tenant: Annotated[str, Depends(require_tenant)]) -> dict:
        verdict = evaluate_submission(sub)
        store = _get_store()
        now = datetime.now(timezone.utc)
        ev = EventEnvelope(
            event_id=uuid4(),
            tenant_id=tenant,
            trace_id=f"trial-{sub.evaluator}-{now.strftime('%Y%m%d-%H%M%S')}",
            object_id=sub.object_id,
            object_type=ObjectType.TRANSPORT_TASK,
            occurred_at=sub.started_at,
            ingested_at=now,
            type=EventType.PROJECTION_ONLY,
            payload={
                "material_kind": "trial_record",
                "data": {
                    "evaluator": sub.evaluator,
                    "role": sub.role,
                    "object_id": sub.object_id,
                    "elapsed_seconds": sub.elapsed_seconds,
                    "scores": verdict["recorded_scores"],
                    "average_score_milli": verdict["average_score_milli"],
                    "within_30s": verdict["within_30s"],
                    "would_rely": sub.would_rely,
                    "passed": verdict["passed"],
                    "comments": sub.comments,
                },
            },
            provenance=Provenance(source_system="disclosure-trial", operator=sub.evaluator),
        )
        result = store.append(ev)
        return {
            "event_id": str(ev.event_id),
            "seq": result.seq,
            "duplicate": result.duplicate,
            **verdict,
            "time_limit_seconds": TIME_LIMIT_SECONDS,
            "pass_rule": f"{TIME_LIMIT_SECONDS}s 内出示完整证据链 且 平均分 ≥ {PASS_AVG_MILLI / 1000:.1f}",
        }

    @router.get("/trial/records")
    def records(tenant: Annotated[str, Depends(require_tenant)], limit: int = 50) -> dict:
        store = _get_store()
        out = []
        for oid in store.iter_objects(tenant):
            for ev in store.read_object_events(tenant, oid):
                if ev.type is EventType.PROJECTION_ONLY and ev.payload.get("material_kind") == "trial_record":
                    d = dict(ev.payload["data"])
                    d["event_id"] = str(ev.event_id)
                    d["recorded_at"] = ev.occurred_at.isoformat()
                    out.append(d)
        out.sort(key=lambda r: r["recorded_at"], reverse=True)
        passed = sum(1 for r in out if r["passed"])
        return {
            "total": len(out),
            "passed": passed,
            "pass_rate_milli": (passed * 1000 // len(out)) if out else 0,
            "records": out[:limit],
        }

    return router
