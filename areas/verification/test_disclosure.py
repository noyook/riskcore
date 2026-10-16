"""W-007 disclosure tests — explain API 全量协议。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from disclosure import ExplainService
from eventlog import SqliteStore
from eventlog.projector import SqliteProjector
from executor import ExecutorService
from riskfn import load_policy

from .factories import (
    AS_OF,
    TENANT,
    expert_assertion_event,
    inspection_event,
    manual_override_event,
    registry_event,
    telemetry_event,
    violation_event,
    work_order_created,
)

EARLY = AS_OF - timedelta(days=2)


@pytest.fixture
def disclosure(tmp_path):
    store = SqliteStore(tmp_path / "factlog.db")
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, TENANT)
    executor = ExecutorService(projector, policy, state_db_path=str(tmp_path / "executor.db"))
    svc = ExplainService(store, projector, executor, policy, TENANT)
    app = TestClient.__module__  # noqa - placeholder to keep import used
    yield store, projector, executor, svc
    store.close()


def _app(svc: ExplainService):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(svc.build_router())
    return TestClient(app)


def test_explain_protocol_shape(disclosure) -> None:
    store, projector, executor, svc = disclosure
    store.append(violation_event("veh-1", EARLY, kind="speeding", count=3))
    store.append(telemetry_event("veh-1", EARLY, continuous_driving_minutes=310))
    projector.project_object("veh-1", AS_OF)

    resp = _app(svc).get("/explain", params={"object_id": "veh-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object_id"] == "veh-1"
    assert body["current"]["r_milli"] > 0
    assert body["current"]["policy_id"] == "v1"
    assert set(body["confidence"]) >= {"n_events", "window_coverage_milli", "data_freshness_milli"}
    groups = {g["group"] for g in body["evidence_chain"]}
    assert "factors" in groups
    # 每条证据带业务语言 + 下钻入口
    ev = body["evidence_chain"][0]["events"][0]
    assert "超速" in ev["plain_text"] and ev["event_id"]
    assert ev["provenance"]["source_system"]


def test_veto_evidence_group_and_conclusion(disclosure) -> None:
    store, projector, executor, svc = disclosure
    store.append(registry_event("veh-2", EARLY, registry_kind="tank_inspection",
                                expires_on=__import__("datetime").date(2026, 10, 1), status="expired"))
    projector.project_object("veh-2", AS_OF)
    body = svc.explain("veh-2").model_dump(mode="json")
    assert "veto" in {g["group"] for g in body["evidence_chain"]}
    assert "罐检逾期" in body["conclusion"]
    assert body["current"]["veto_active"] is True


def test_override_banner_is_top_and_visible(disclosure) -> None:
    store, projector, executor, svc = disclosure
    store.append(violation_event("veh-3", EARLY, kind="speeding", count=3))
    store.append(manual_override_event("veh-3", EARLY, override_level="clear",
                                       reason="客户紧急", approver="supervisor-1"))
    projector.project_object("veh-3", AS_OF)
    body = svc.explain("veh-3").model_dump(mode="json")
    assert body["evidence_chain"][0]["group"] == "override"  # 置顶
    assert body["evidence_chain"][0]["banner"] is True
    assert "人工调整" in body["conclusion"]


def test_active_intervention_card_with_release_condition(disclosure) -> None:
    store, projector, executor, svc = disclosure
    # 罐检逾期 → veto → lock 干预卡
    store.append(registry_event("veh-4", EARLY, registry_kind="tank_inspection",
                                expires_on=__import__("datetime").date(2026, 10, 1), status="expired"))
    projector.project_object("veh-4", AS_OF)
    executor.evaluate("veh-4", "allow_dispatch", AS_OF + timedelta(minutes=1))

    body = svc.explain("veh-4").model_dump(mode="json")
    cards = body["active_interventions"]
    assert cards and cards[0]["predicate"] == "allow_dispatch"
    assert cards[0]["clause"]
    assert "r < 750" in cards[0]["release_condition"]  # 800 - 50 滞后带，业务语言


def test_low_confidence_flag_and_calibration_notes(disclosure) -> None:
    store, projector, executor, svc = disclosure
    # 只有一条陈旧证据 → 覆盖率/新鲜度低
    store.append(violation_event("veh-5", AS_OF - timedelta(days=120), kind="speeding", count=1))
    projector.project_object("veh-5", AS_OF)
    body = svc.explain("veh-5").model_dump(mode="json")
    assert body["low_confidence"] is True
    assert any("未经充分校准" in n for n in body["confidence"]["calibration_notes"])


def test_prior_injection_shown_as_own_group(disclosure) -> None:
    store, projector, executor, svc = disclosure
    store.append(expert_assertion_event("drv-9", EARLY, L=6, E=5, C=6, valid_until=__import__("datetime").date(2027, 1, 1)))
    projector.project_object("drv-9", AS_OF)
    body = svc.explain("drv-9").model_dump(mode="json")
    assert "prior" in {g["group"] for g in body["evidence_chain"]}
    assert "LEC-D" in body["evidence_chain"][0]["events"][0]["plain_text"]


def test_unknown_object_returns_no_decision(disclosure) -> None:
    _store, _projector, _executor, svc = disclosure
    body = svc.explain("nope").model_dump(mode="json")
    assert body["current"]["r_milli"] is None
    assert "fail-closed" in body["conclusion"]
