"""W-005 reconciler tests — 对账守护（铁律三：缓存不是真相）。

Acceptance: 主动损坏缓存后，守护进程检出差异并以重算结果修正。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from eventlog import EventType
from eventlog.projector import SqliteProjector
from eventlog.reconciler import Reconciler
from riskfn import load_policy

from .factories import AS_OF, TENANT, telemetry_event, violation_event

EARLY = AS_OF - timedelta(days=2)


@pytest.fixture
def policy():
    return load_policy("v1")


@pytest.fixture
def proj(store, policy):
    p = SqliteProjector(store, policy, TENANT)
    yield p
    p.close()


@pytest.fixture
def reconciler(store, proj):
    return Reconciler(store, proj, TENANT)


def seed(store, n_objects: int = 3) -> None:
    for i in range(n_objects):
        store.append(violation_event(f"veh-{i}", EARLY, kind="speeding", count=2 + i))
        store.append(telemetry_event(f"veh-{i}", EARLY, continuous_driving_minutes=300))


def test_fresh_projection_has_no_drift(store, proj, reconciler) -> None:
    seed(store)
    proj.project_all(AS_OF)
    reports = reconciler.reconcile_full(AS_OF)
    assert len(reports) == 3
    assert all(not r.drifted for r in reports)


def test_corrupted_cache_is_detected_and_fixed(store, proj, reconciler) -> None:
    seed(store)
    proj.project_all(AS_OF)
    good = proj.read_decision("veh-1").r_milli
    assert good > 0

    # 主动损坏缓存（缓存表允许写——它不是本体；日志永不可犯）
    proj._conn.execute(
        "UPDATE decision_cache SET r_milli=999 WHERE tenant_id=? AND object_id='veh-1'",
        (TENANT,),
    )
    proj._conn.commit() if hasattr(proj._conn, "commit") else None
    assert proj.read_decision("veh-1").r_milli == 999

    report = reconciler.reconcile_object("veh-1", AS_OF, mode="sample")
    assert report.drifted is True
    assert report.fixed is True
    assert report.r_cached == 999
    assert report.r_recomputed == good
    # 以重算为准修正
    assert proj.read_decision("veh-1").r_milli == good


def test_drift_recorded_as_projection_only_event(store, proj, reconciler) -> None:
    seed(store, n_objects=1)
    proj.project_all(AS_OF)
    proj._conn.execute(
        "UPDATE decision_cache SET r_milli=1 WHERE tenant_id=? AND object_id='veh-0'", (TENANT,)
    )
    reconciler.reconcile_object("veh-0", AS_OF, mode="sample")

    records = [
        e for e in store.read_object_events(TENANT, "veh-0")
        if e.type is EventType.PROJECTION_ONLY
        and e.payload.get("material_kind") == "reconciliation_run"
    ]
    assert len(records) == 1
    assert records[0].payload["data"]["drifted"] is True
    assert records[0].payload["data"]["r_cached"] == 1


def test_reconciliation_record_does_not_change_facts(store, proj, reconciler) -> None:
    seed(store, n_objects=1)
    proj.project_all(AS_OF)
    r0 = proj.read_decision("veh-0").r_milli
    reconciler.reconcile_full(AS_OF)  # 无漂移也应留痕？无漂移不记录
    proj.project_all(AS_OF)
    assert proj.read_decision("veh-0").r_milli == r0


def test_sample_selection_is_deterministic(store, proj, reconciler) -> None:
    seed(store, n_objects=6)
    proj.project_all(AS_OF)
    first = [r.object_id for r in reconciler.reconcile_sample(AS_OF, n=3, seed=42)]
    second = [r.object_id for r in reconciler.reconcile_sample(AS_OF, n=3, seed=42)]
    assert first == second and len(first) == 3


def test_full_rebuild_equals_incremental_projection(store, proj, reconciler) -> None:
    """对账差异为零：全量重放与增量投影一致。"""
    seed(store, n_objects=4)
    proj.project_all(AS_OF)
    before = {oid: proj.read_decision(oid).r_milli for oid in store.iter_objects(TENANT)}
    proj.rebuild_all(AS_OF)
    after = {oid: proj.read_decision(oid).r_milli for oid in store.iter_objects(TENANT)}
    assert before == after
