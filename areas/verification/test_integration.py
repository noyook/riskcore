"""End-to-end golden path: ingest → log → projector → executor → verdict.

Uses the real SQLite store, the real projector and the real executor service
(no mocks except the HTTP boundary), exercising the M1 kernel as one chain.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eventlog import SqliteStore
from eventlog.app import create_app
from eventlog.projector import SqliteProjector
from executor import ExecutorService
from riskfn import load_policy

from .factories import (
    AS_OF,
    TENANT,
    correction_event,
    expert_assertion_event,
    inspection_event,
    registry_event,
    telemetry_event,
    violation_event,
    work_order_closed,
    work_order_created,
)

EARLY = AS_OF - timedelta(days=2)
NOW = AS_OF + timedelta(minutes=1)


@pytest.fixture
def kernel(tmp_path):
    store = SqliteStore(tmp_path / "factlog.db")
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, TENANT)
    executor = ExecutorService(projector, policy, state_db_path=str(tmp_path / "executor.db"))
    yield store, projector, executor
    store.close()


def test_golden_path_veto_lock_with_evidence(kernel) -> None:
    store, projector, executor = kernel
    # 罐检逾期（registry 否决载体）+ 超速违法 + 疲劳遥测
    store.append(registry_event("veh-1", EARLY, registry_kind="tank_inspection",
                                expires_on=__import__("datetime").date(2026, 10, 1), status="expired"))
    store.append(violation_event("veh-1", EARLY, kind="speeding", count=3))
    store.append(telemetry_event("veh-1", EARLY, continuous_driving_minutes=300))

    projection = projector.project_object("veh-1", AS_OF)
    assert projection.decision.r_milli == 1000
    assert projection.decision.veto_active is True
    assert projection.snapshot.veto_flags == ["tank_inspection_overdue"]

    verdict = executor.evaluate("veh-1", "allow_dispatch", NOW)
    assert verdict.intervention == "lock"
    assert verdict.reason_code == "veto_active"
    assert verdict.clause  # R5 条款
    assert verdict.explain_refs  # R5 证据（可下钻到原始事件）
    assert verdict.enforced is False  # W-006 旁路观察期
    assert verdict.allow is True  # 只记不拦
    assert executor.observations.count() == 1

    # 证据可下钻：explain_refs 里的每个 event_id 都能在日志中找到
    for ref in verdict.explain_refs:
        assert store.get(TENANT, ref) is not None


def test_golden_path_rectification_releases_lock(kernel) -> None:
    store, projector, executor = kernel
    store.append(inspection_event("veh-2", EARLY, hazard_level="major", hazard_ref="hz-1"))
    projector.project_object("veh-2", AS_OF)
    v1 = executor.evaluate("veh-2", "lock_vehicle", NOW)
    assert v1.intervention == "lock" and v1.reason_code == "veto_active"

    # 整改闭环：建工单 → 闭环
    store.append(work_order_created("veh-2", EARLY, hazard_ref="hz-1"))
    store.append(work_order_closed("veh-2", EARLY, closes_order_id="hz-1"))
    projector.project_object("veh-2", AS_OF + timedelta(minutes=1))
    v2 = executor.evaluate("veh-2", "lock_vehicle", NOW + timedelta(minutes=1))
    assert v2.intervention == "released" or v2.intervention == "none"
    assert v2.reason_code in ("released", "hysteresis_hold", "ok")


def test_golden_path_correction_chain_end_to_end(kernel) -> None:
    store, projector, executor = kernel
    # 误录的超速 5 次 → correction 改为 1 次
    bad = violation_event("veh-3", EARLY, kind="speeding", count=5)
    store.append(bad)
    r_before = projector.project_object("veh-3", AS_OF).decision.r_milli

    store.append(correction_event(
        "veh-3", EARLY + timedelta(minutes=5), corrects_event_id=bad.event_id,
        corrected_payload={"violation_kind": "speeding", "count": 1, "duration_minutes": 10,
                           "detected_at": EARLY.isoformat()},
    ))
    r_after = projector.project_object("veh-3", AS_OF + timedelta(minutes=6)).decision.r_milli
    assert r_after < r_before

    verdict = executor.evaluate("veh-3", "allow_dispatch", NOW + timedelta(minutes=6))
    assert verdict.intervention == "none"


def test_ingest_api_then_project(kernel) -> None:
    """HTTP 写入 → 投影 → 执行，全链路经真实 ingest 边界。"""
    from fastapi.testclient import TestClient

    store, projector, executor = kernel
    app = create_app(store)
    client = TestClient(app)

    ev = violation_event("veh-4", EARLY, kind="fatigue", count=2)
    resp = client.post(
        "/events",
        json=ev.model_dump(mode="json"),
        headers={"X-Tenant-Id": TENANT},
    )
    assert resp.status_code == 200
    assert resp.json()["seq"] == 1

    # outbox relay 投递
    from eventlog.relay import relay_once

    stats = relay_once(store, lambda row: None)
    assert stats["delivered"] == 1

    projector.project_object("veh-4", AS_OF)
    verdict = executor.evaluate("veh-4", "allow_dispatch", NOW)
    assert verdict.r_milli is not None
    assert verdict.intervention in ("none", "alert", "restrict", "lock")
