#!/usr/bin/env python3
"""Single-deployable entrypoint (D-005): one process mounts every area router.

    python3 scripts/serve.py [--db PATH] [--port 8000]

Routers:
    /events, /events:batch   ingest (event-log area — the only write path)
    /evaluate, /observations executor (W-006 bypass observe mode)
    /health, /ready          shared liveness/readiness

A background projector tick advances facts/decisions; as_of is injected from
the wall clock ONLY at this scheduler boundary (the projector body never
reads one).
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for area in ("event-log", "risk-function", "executor", "disclosure", "projection"):
    p = ROOT / "areas" / area
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


def main() -> None:
    import os

    # ---- 配置：全部来自环境变量，启动时集中校验、快速失败 ----------------
    def env(name: str, default: str) -> str:
        v = os.environ.get(name, default)
        if not v:
            raise SystemExit(f"config error: {name} must not be empty")
        return v

    def env_int(name: str, default: int) -> int:
        try:
            return int(env(name, str(default)))
        except ValueError:
            raise SystemExit(f"config error: {name} must be an integer")

    db = env("RISKCORE_DB", str(ROOT / ".tmp" / "riskcore.db"))
    tenant = env("RISKCORE_TENANT", "t1")
    port = env_int("RISKCORE_PORT", 8000)
    tick_seconds = env_int("RISKCORE_TICK_SECONDS", 60)
    predicate_mode = env("RISKCORE_PREDICATE_MODE", "observe")
    if predicate_mode not in ("observe", "enforce"):
        raise SystemExit("config error: RISKCORE_PREDICATE_MODE must be observe|enforce")

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=db)
    parser.add_argument("--port", type=int, default=port)
    parser.add_argument("--tick-seconds", type=int, default=tick_seconds)
    args = parser.parse_args()
    if predicate_mode == "enforce":
        print(
            "[config] RISKCORE_PREDICATE_MODE=enforce — 真拦截已开启（W-010 切流态）；"
            "回滚：RISKCORE_PREDICATE_MODE=observe 或 scripts/rollback_observe.sh"
        )

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    from eventlog import SqliteStore
    from eventlog.app import create_app
    from eventlog.migrate_api import build_migrate_router
    from eventlog.projector import SqliteProjector
    from eventlog.reconciler import Reconciler
    from eventlog.trial_api import build_trial_router
    from executor import ExecutorService
    from riskfn import load_policy
    from disclosure import ExplainService
    from projection import ProjectionService

    store = SqliteStore(args.db)
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, tenant)
    executor = ExecutorService(projector, policy, state_db_path=args.db,
                               predicate_mode=predicate_mode)
    explain_service = ExplainService(store, projector, executor, policy, tenant)
    projection_service = ProjectionService(store, projector, executor, policy, tenant)

    app = create_app(store)
    app.include_router(executor.build_router())
    app.include_router(explain_service.build_router())
    app.include_router(projection_service.build_router())
    app.include_router(build_migrate_router())
    app.include_router(build_trial_router())

    static_dir = ROOT / "areas" / "disclosure" / "disclosure" / "static"
    app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")
    migrate_dir = ROOT / "areas" / "event-log" / "eventlog" / "static"
    app.mount("/migrate-ui", StaticFiles(directory=str(migrate_dir), html=True), name="migrate-ui")

    # projector + reconciler scheduler (as_of injected at the boundary only)
    stop = threading.Event()

    def tick() -> None:
        while not stop.is_set():
            try:
                as_of = datetime.now(timezone.utc)
                projector.project_all(as_of)
                Reconciler(store, projector, tenant).reconcile_sample(as_of, n=5)
            except Exception as exc:  # never kill the scheduler thread
                print(f"[scheduler] tick failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            stop.wait(args.tick_seconds)

    threading.Thread(target=tick, daemon=True).start()

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
