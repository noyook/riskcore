"""Migration API — paper-form ingestion boundary (W-002).

Endpoints (all write through the standard append path; tenant header enforced):
  POST /migrate/parse    dry-run: validate pasted rows, no writes
  POST /migrate/ingest   commit drafts → expert_assertion events + report
  GET  /migrate/template 《车辆运输危险源清单》1.1–1.10 模板（默认 L/E/C）
  GET  /migrate/report?batch_id=…   regenerate the batch report from the log

The report is derived from the log (trace_id carries the batch id) — no new
current-value tables are created for migration bookkeeping.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .envelope import EventEnvelope
from .migrate import (
    VEHICLE_HAZARD_TEMPLATE,
    AssertionDraft,
    MigrationReport,
    batch_correction_count,
    ingest_drafts,
    parse_tsv,
)
from .store import EventLogStore

TENANT_HEADER = "X-Tenant-Id"


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    assessed_at: date
    valid_until: date
    object_type: str = "vehicle"
    default_measures: str = "见纸质记录表"


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drafts: list[AssertionDraft]
    batch_id: str | None = None


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    object_id: str
    corrects_event_id: str
    reason: str = Field(min_length=1)
    corrected_payload: dict


def require_tenant(x_tenant_id: Annotated[str | None, Header()] = None) -> str:
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="missing tenant: X-Tenant-Id header required")
    return x_tenant_id


def _get_store() -> EventLogStore:
    from .app import get_ingest_state

    return get_ingest_state().store


def build_migrate_router() -> APIRouter:
    router = APIRouter(tags=["migrate"])

    @router.get("/migrate/template")
    def template() -> dict:
        return {
            "source": "《车辆运输危险源清单》1.1–1.10",
            "items": [
                {"code": i.code, "name": i.name, "L": i.L, "E": i.E, "C": i.C, "control_hint": i.control_hint}
                for i in VEHICLE_HAZARD_TEMPLATE
            ],
            "paste_format": "object_id, [条目号,] 危险源, L, E, C, [管控措施,] [有效期]（Tab 或逗号分隔，# 开头为注释）",
        }

    @router.post("/migrate/parse")
    def parse(req: ParseRequest) -> dict:
        result = parse_tsv(
            req.text,
            operator=req.operator,
            assessed_at=req.assessed_at,
            valid_until=req.valid_until,
            object_type=req.object_type,
            default_measures=req.default_measures,
        )
        return {
            "ok": result.ok,
            "drafts": [d.model_dump(mode="json") for d in result.drafts],
            "errors": [e.model_dump(mode="json") for e in result.errors],
        }

    @router.post("/migrate/ingest")
    def ingest(req: IngestRequest, tenant: Annotated[str, Depends(require_tenant)]) -> dict:
        store = _get_store()
        report = ingest_drafts(
            store,
            req.drafts,
            tenant_id=tenant,
            operator=req.drafts[0].operator if req.drafts else "unknown",
            batch_id=req.batch_id,
        )
        return {
            "batch_id": report.batch_id,
            "summary": report.summary(),
            "rows": [
                {
                    "object_id": r.object_id,
                    "item_code": r.item_code,
                    "hazard_source": r.hazard_source,
                    "event_id": r.event_id,
                    "seq": r.seq,
                    "status": r.status,
                    "error": r.error,
                }
                for r in report.rows
            ],
        }

    @router.post("/migrate/correction")
    def correction(req: CorrectionRequest, tenant: Annotated[str, Depends(require_tenant)]) -> dict:
        """人工修正项：以 correction 事件修订已迁移的断言（原事件不动）。"""
        store = _get_store()
        target = store.get(tenant, req.corrects_event_id)
        if target is None:
            raise HTTPException(status_code=422, detail=f"correction target {req.corrects_event_id} not found")
        if target.type.value != "expert_assertion":
            raise HTTPException(status_code=422, detail="correction target must be an expert_assertion event")
        import uuid as _uuid

        ev = EventEnvelope(
            event_id=_uuid.uuid4(),
            tenant_id=tenant,
            trace_id=f"migrate-{req.batch_id}",
            object_id=req.object_id,
            object_type=target.object_type,
            occurred_at=datetime.now(timezone.utc),
            ingested_at=datetime.now(timezone.utc),
            type="correction",
            payload={
                "corrects_event_id": req.corrects_event_id,
                "reason": req.reason,
                "corrected_payload": req.corrected_payload,
            },
            provenance={"source_system": "historical-migration", "operator": target.provenance.operator or "auditor"},
        )
        result = store.append(ev)
        return {"event_id": str(ev.event_id), "seq": result.seq, "duplicate": result.duplicate}

    @router.get("/migrate/report")
    def report(batch_id: str, tenant: Annotated[str, Depends(require_tenant)]) -> dict:
        store = _get_store()
        corrections = batch_correction_count(store, tenant, batch_id)
        # 从日志重建该批次的事件清单（trace_id 携带 batch）
        corrected_ids: set[str] = set()
        for oid in store.iter_objects(tenant):
            for ev in store.read_object_events(tenant, oid):
                if ev.type.value == "correction" and ev.trace_id == f"migrate-{batch_id}":
                    corrected_ids.add(str(ev.payload["corrects_event_id"]))
        rows = []
        for oid in store.iter_objects(tenant):
            for ev in store.read_object_events(tenant, oid):
                if ev.trace_id == f"migrate-{batch_id}" and ev.type.value == "expert_assertion":
                    rows.append(
                        {
                            "object_id": oid,
                            "event_id": str(ev.event_id),
                            "hazard_source": ev.payload["hazard_source"],
                            "D": ev.payload["L"] * ev.payload["E"] * ev.payload["C"],
                            "valid_until": ev.payload["valid_until"],
                            "occurred_at": ev.occurred_at.isoformat(),
                            "corrected": str(ev.event_id) in corrected_ids,
                        }
                    )
        return {
            "batch_id": batch_id,
            "assertion_events": len(rows),
            "corrections": corrections,
            "rows": rows,
        }

    return router
