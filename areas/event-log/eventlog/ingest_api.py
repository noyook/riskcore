"""Ingest boundary — the only write path into the fact log.

Boundary rules (event-schema.md + area rules):
- tenant_id comes from the X-Tenant-Id header and is server-enforced:
  a bare call without the header is 401, no bypass switch;
- envelope.tenant_id must match the header (403 otherwise);
- seq is never accepted from the client: the store assigns it;
- ingested_at is stamped at the boundary (the only place a wall clock is
  read on the write path — the pure function and projector never read one);
- everything is validated by the envelope models before touching the store.

This router is mounted by the single deployable (D-005); disclosure/projection
routers are read-only and live in their own areas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from .envelope import EventEnvelope
from .store import EventLogStore

TENANT_HEADER = "X-Tenant-Id"


class EventAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    seq: int
    duplicate: bool


class BatchAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[EventAccepted]


class IngestState:
    """Shared app state: the store plus an injectable clock (tests)."""

    def __init__(self, store: EventLogStore, clock=lambda: datetime.now(timezone.utc)) -> None:
        self.store = store
        self.clock = clock


def get_ingest_state() -> IngestState:
    from .app import get_ingest_state as _get  # app wires the singleton

    return _get()


def require_tenant(x_tenant_id: Annotated[str | None, Header()] = None) -> str:
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="missing tenant: X-Tenant-Id header required")
    return x_tenant_id


def build_ingest_router() -> APIRouter:
    router = APIRouter(tags=["ingest"])

    @router.post("/events", response_model=EventAccepted)
    def post_event(
        event: EventEnvelope,
        tenant: Annotated[str, Depends(require_tenant)],
    ) -> EventAccepted:
        if event.tenant_id != tenant:
            raise HTTPException(status_code=403, detail="tenant mismatch between header and envelope")
        if event.seq is not None:
            raise HTTPException(status_code=422, detail="seq is server-assigned; clients must not transmit it")
        state = get_ingest_state()
        stamped = event.model_copy(update={"ingested_at": state.clock(), "seq": None})
        result = state.store.append(stamped)
        return EventAccepted(event_id=result.event_id, seq=result.seq, duplicate=result.duplicate)

    @router.post("/events:batch", response_model=BatchAccepted)
    def post_events(
        events: list[EventEnvelope],
        tenant: Annotated[str, Depends(require_tenant)],
    ) -> BatchAccepted:
        if not events:
            raise HTTPException(status_code=422, detail="empty batch")
        state = get_ingest_state()
        for e in events:
            if e.tenant_id != tenant:
                raise HTTPException(status_code=403, detail="tenant mismatch between header and envelope")
            if e.seq is not None:
                raise HTTPException(status_code=422, detail="seq is server-assigned; clients must not transmit it")
        stamped = [
            e.model_copy(update={"ingested_at": state.clock(), "seq": None}) for e in events
        ]
        results = state.store.append_batch(stamped)
        return BatchAccepted(
            results=[EventAccepted(event_id=r.event_id, seq=r.seq, duplicate=r.duplicate) for r in results]
        )

    return router
