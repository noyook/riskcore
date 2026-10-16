"""W-005 projector tests — facts, vetoes, correction 链, watermark, 重建。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from eventlog import EventType, SqliteStore
from eventlog.projector import SqliteProjector
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


@pytest.fixture
def policy():
    return load_policy("v1")


@pytest.fixture
def proj(store: SqliteStore, policy):
    p = SqliteProjector(store, policy, TENANT)
    yield p
    p.close()


def project(proj: SqliteProjector, object_id: str, as_of: datetime = AS_OF):
    return proj.project_object(object_id, as_of)


# ------------------------------------------------------------------- facts


def test_telemetry_breach_creates_facts(store, proj) -> None:
    store.append(telemetry_event("veh-1", EARLY, continuous_driving_minutes=300, speed_max_kmh=120))
    store.append(telemetry_event("veh-1", EARLY, continuous_driving_minutes=60, speed_max_kmh=60))
    out = project(proj, "veh-1")
    f_fatigue = out.snapshot.facts["telemetry_fatigue_windows"]
    f_speed = out.snapshot.facts["telemetry_speeding_windows"]
    assert f_fatigue.freshness == "valid" and len(f_fatigue.contributions) == 1
    assert f_speed.freshness == "valid" and len(f_speed.contributions) == 1
    assert out.decision.r_milli > 0


def test_violation_counts_and_decay(store, proj) -> None:
    store.append(violation_event("veh-1", EARLY, kind="speeding", count=3))
    out = project(proj, "veh-1")
    fact = out.snapshot.facts["violation_speeding"]
    assert fact.freshness == "valid"
    assert fact.contributions[0].value == 3
    assert fact.contributions[0].age_days == 2


def test_open_hazard_veto_and_closure(store, proj) -> None:
    store.append(inspection_event("veh-1", EARLY, hazard_level="major", hazard_ref="hz-1"))
    out = project(proj, "veh-1")
    assert out.snapshot.veto_flags == ["major_hazard_open"]
    assert out.decision.r_milli == 1000 and out.decision.veto_active is True

    store.append(work_order_created("veh-1", EARLY, hazard_ref="hz-1"))
    store.append(work_order_closed("veh-1", EARLY, closes_order_id="hz-1"))
    out2 = project(proj, "veh-1")
    assert out2.snapshot.veto_flags == []
    assert out2.decision.r_milli < 1000
    assert out2.snapshot.facts["open_hazard_major"].freshness == "missing"


def test_registry_expiry_veto(store, proj) -> None:
    store.append(registry_event("veh-1", EARLY, registry_kind="tank_inspection",
                                expires_on=date(2026, 10, 1), status="expired"))
    out = project(proj, "veh-1")
    assert out.snapshot.veto_flags == ["tank_inspection_overdue"]
    assert out.decision.r_milli == 1000


def test_registry_valid_expiry_factor(store, proj) -> None:
    store.append(registry_event("veh-1", EARLY, registry_kind="license",
                                expires_on=date(2026, 10, 25), status="valid"))
    out = project(proj, "veh-1")
    assert out.snapshot.veto_flags == []
    fact = out.snapshot.facts["license_days_remaining"]
    # (2026-10-25 00:00Z - 2026-10-15 12:00Z).days = 9（floor）
    assert fact.days_remaining == 9 and fact.freshness == "valid"
    assert out.decision.r_milli > 0  # 9 days inside 60-day warn window


def test_registry_supersession_latest_wins(store, proj) -> None:
    store.append(registry_event("veh-1", EARLY, registry_kind="license",
                                expires_on=date(2026, 10, 1), status="valid"))
    store.append(registry_event("veh-1", EARLY + timedelta(hours=1), registry_kind="license",
                                expires_on=date(2028, 1, 1), status="valid"))
    out = project(proj, "veh-1")
    fact = out.snapshot.facts["license_days_remaining"]
    assert fact.days_remaining == (date(2028, 1, 1) - AS_OF.date()).days - 1  # 12:00 floor
    assert out.snapshot.veto_flags == []


def test_expert_assertion_prior_and_expiry(store, proj) -> None:
    store.append(expert_assertion_event("drv-1", EARLY, L=5, E=4, C=5, valid_until=date(2027, 1, 1)))
    out = project(proj, "drv-1")
    prior = out.snapshot.facts["lecd_prior"]
    assert prior.freshness == "valid" and prior.prior_r_milli == 100  # D = L*E*C
    # 无任何传感器数据 → 先验注入
    assert out.decision.prior_injected is True
    assert out.decision.r_milli == 20  # 100 * 200 / 1000

    # 已过期的断言单独成对象：无有效先验 → 不注入
    store.append(expert_assertion_event("drv-2", EARLY, L=5, E=4, C=5, valid_until=date(2026, 10, 1)))
    out2 = project(proj, "drv-2")
    assert out2.snapshot.facts["lecd_prior"].freshness == "missing"
    assert out2.decision.prior_injected is False
    assert out2.decision.r_milli == 0


# -------------------------------------------------------------- correction


def test_correction_chain_equivalent_to_event_never_happening(store, proj) -> None:
    """属性：更正后输出与"原事件未发生"一致（除 provenance / event_id）。"""
    # stream A: a major hazard that gets corrected down to minor
    a = inspection_event("veh-1", EARLY, hazard_level="major", hazard_ref="hz-9")
    store.append(a)
    store.append(correction_event(
        "veh-1", EARLY + timedelta(minutes=5), corrects_event_id=a.event_id,
        corrected_payload={
            "hazard_level": "minor", "location": "depot-A", "photo_refs": [],
            "inspected_at": EARLY.isoformat(), "hazard_ref": "hz-9",
        },
    ))
    out_a = project(proj, "veh-1")

    # stream B: the same object with only a minor hazard (as if major never existed)
    store.append(inspection_event("veh-2", EARLY, hazard_level="minor", hazard_ref="hz-9"))
    out_b = project(proj, "veh-2")

    assert out_a.snapshot.veto_flags == out_b.snapshot.veto_flags == []
    assert out_a.decision.r_milli == out_b.decision.r_milli
    assert out_a.decision.veto_active == out_b.decision.veto_active
    assert out_a.snapshot.facts["open_hazard_major"].freshness == "missing"
    # minor 隐患不计分（字典只收 major/general）
    assert out_a.decision.r_milli == 0


def test_latest_correction_wins(store, proj) -> None:
    a = violation_event("veh-1", EARLY, kind="speeding", count=2)
    store.append(a)
    store.append(correction_event("veh-1", EARLY, corrects_event_id=a.event_id,
        corrected_payload={"violation_kind": "speeding", "count": 4, "duration_minutes": 10,
                           "detected_at": EARLY.isoformat()}))
    store.append(correction_event("veh-1", EARLY, corrects_event_id=a.event_id,
        corrected_payload={"violation_kind": "speeding", "count": 1, "duration_minutes": 10,
                           "detected_at": EARLY.isoformat()}))
    out = project(proj, "veh-1")
    assert out.snapshot.facts["violation_speeding"].contributions[0].value == 1


# -------------------------------------------------------------- watermark


def test_watermark_advances_and_late_events_wait(store, proj) -> None:
    store.append(telemetry_event("veh-1", EARLY))
    project(proj, "veh-1")
    assert store.watermark(TENANT, "veh-1") == 1

    late = telemetry_event("veh-1", occurred_at=EARLY - timedelta(days=5), ingested_at=AS_OF)
    store.append(late)
    assert store.watermark(TENANT, "veh-1") == 1  # 未投影前不回溯
    project(proj, "veh-1")
    assert store.watermark(TENANT, "veh-1") == 2


def test_future_event_not_observable(store, proj) -> None:
    store.append(violation_event("veh-1", occurred_at=AS_OF + timedelta(days=1), ingested_at=AS_OF))
    out = project(proj, "veh-1")
    assert out.snapshot.facts["violation_speeding"].freshness == "missing"
    assert out.decision.r_milli == 0


# ------------------------------------------------------------------ cache


def test_read_decision_roundtrip(store, proj) -> None:
    store.append(violation_event("veh-1", EARLY, kind="speeding", count=3))
    out = project(proj, "veh-1")
    cached = proj.read_decision("veh-1")
    assert cached is not None
    assert cached.r_milli == out.decision.r_milli
    assert cached.policy_id == "v1"
    snap = proj.read_snapshot("veh-1")
    assert snap is not None and "violation_speeding" in snap.facts


def test_rebuild_all_matches_incremental(store, proj) -> None:
    for i in range(3):
        store.append(violation_event(f"veh-{i}", EARLY, kind="speeding", count=2 + i))
    store.append(telemetry_event("veh-1", EARLY, continuous_driving_minutes=300))
    first = {oid: project(proj, oid).decision.r_milli for oid in ("veh-0", "veh-1", "veh-2")}

    proj.rebuild_all(AS_OF)  # SOP：清缓存 + 全量重放
    second = {oid: proj.read_decision(oid).r_milli for oid in ("veh-0", "veh-1", "veh-2")}
    assert first == second
