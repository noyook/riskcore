"""Single-deployable shell for the event-log area (D-005: no microservices).

create_app() wires the ingest router plus the shared health endpoints.
The disclosure and executor routers are mounted by the top-level entrypoint
(scripts/serve.py) — one process, one deployable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .ingest_api import IngestState, build_ingest_router
from .store import EventLogStore

_default_state: IngestState | None = None


def configure(store: EventLogStore, clock=lambda: datetime.now(timezone.utc)) -> IngestState:
    global _default_state
    _default_state = IngestState(store, clock)
    return _default_state


def get_ingest_state() -> IngestState:
    if _default_state is None:
        raise RuntimeError("ingest state not configured; call eventlog.app.configure(store) first")
    return _default_state


def create_app(store: EventLogStore | None = None, clock=lambda: datetime.now(timezone.utc)) -> FastAPI:
    app = FastAPI(title="riskcore event-log", version="0.1.0")
    if store is not None:
        configure(store, clock)
    app.include_router(build_ingest_router())

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        try:
            state = get_ingest_state()
            state.store.pending_outbox(limit=1)
            return JSONResponse({"status": "ready"})
        except Exception as exc:  # store unreachable → not ready (fail-closed posture)
            return JSONResponse({"status": "not_ready", "detail": type(exc).__name__}, status_code=503)

    return app
