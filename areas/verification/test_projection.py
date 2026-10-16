"""W-008 projection tests — 钢印 / 四色映射 / 三件套 / 重生成幂等。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from eventlog import SqliteStore
from eventlog.projector import SqliteProjector
from executor import ExecutorService
from projection import ProjectionService, assert_stamped, strip_regenerated_at
from riskfn import load_policy

from .factories import (
    AS_OF,
    TENANT,
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
def proj_svc(tmp_path):
    store = SqliteStore(tmp_path / "factlog.db")
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, TENANT)
    executor = ExecutorService(projector, policy, state_db_path=str(tmp_path / "executor.db"))
    svc = ProjectionService(store, projector, executor, policy, TENANT,
                            artifacts_dir=tmp_path / "artifacts")
    yield store, projector, executor, svc
    store.close()


def seed_mixed(store, projector, executor) -> None:
    # 红：罐检逾期 veto
    store.append(registry_event("veh-red", EARLY, registry_kind="tank_inspection",
                                expires_on=date(2026, 10, 1), status="expired"))
    # 橙/黄：违法+遥测（r 由函数定）
    store.append(violation_event("veh-mid", EARLY, kind="speeding", count=2))
    store.append(telemetry_event("veh-mid", EARLY, continuous_driving_minutes=300))
    # 蓝：干净对象
    store.append(telemetry_event("veh-blue", EARLY, continuous_driving_minutes=30))
    # LEC-D 断言
    store.append(expert_assertion_event("drv-1", EARLY, L=6, E=5, C=6, valid_until=date(2027, 1, 1)))
    # 工单：一条闭环、一条逾期
    store.append(inspection_event("veh-mid", EARLY, hazard_level="general", hazard_ref="hz-a"))
    store.append(work_order_created("veh-mid", EARLY, hazard_ref="hz-a", due_date=date(2026, 10, 10)))
    store.append(work_order_closed("veh-mid", EARLY, closes_order_id="hz-a"))
    store.append(work_order_created("veh-red", EARLY, hazard_ref="hz-b", due_date=date(2026, 10, 5)))
    for oid in ("veh-red", "veh-mid", "veh-blue", "drv-1"):
        projector.project_object(oid, AS_OF)
    for oid in ("veh-red", "veh-mid", "veh-blue", "drv-1"):
        executor.evaluate(oid, "allow_dispatch", NOW)


# -------------------------------------------------------------------- stamps


def test_every_artifact_is_stamped(proj_svc) -> None:
    store, projector, executor, svc = proj_svc
    seed_mixed(store, projector, executor)
    for artifact in (
        svc.generate_fourcolor(AS_OF, NOW),
        svc.generate_lecd(AS_OF, NOW),
        svc.generate_doc6(AS_OF, NOW),
        svc.generate_ledger(AS_OF, NOW),
    ):
        assert_stamped(artifact)  # 缺任何一项即抛错（区域规则 1）


# ------------------------------------------------------------------ fourcolor


def test_fourcolor_mapping(proj_svc) -> None:
    store, projector, executor, svc = proj_svc
    seed_mixed(store, projector, executor)
    art = svc.generate_fourcolor(AS_OF, NOW)
    by_id = {e["object_id"]: e for e in art["entities"]}
    assert by_id["veh-red"]["color"] == "red"        # veto → 红
    assert by_id["veh-red"]["control_level"] == "公司级"
    assert by_id["veh-blue"]["color"] == "blue"      # 干净 → 蓝
    assert art["counts"]["red"] >= 1
    assert art["total"] == len(art["entities"])


# ---------------------------------------------------------------------- lecd


def test_lecd_report_chapters_and_markdown(proj_svc) -> None:
    store, projector, executor, svc = proj_svc
    seed_mixed(store, projector, executor)
    art = svc.generate_lecd(AS_OF, NOW)
    assert art["chapter_count"] == 1
    ch = art["chapters"][0]
    assert ch["D"] == 6 * 5 * 6 == 180
    assert ch["object_id"] == "drv-1"
    assert "高度危险" in ch["risk_band"]
    md = __import__("projection.lecd", fromlist=["render_markdown"]).render_markdown(art)
    assert "年度风险辨识评估报告" in md and "D = L(6) × E(5) × C(6) = **180**" in md
    assert "function_version=" in md  # 页脚钢印


# ---------------------------------------------------------------------- doc6


def test_doc6_red_yellow_list(proj_svc) -> None:
    store, projector, executor, svc = proj_svc
    seed_mixed(store, projector, executor)
    art = svc.generate_doc6(AS_OF, NOW)
    assert art["red_count"] >= 1  # veh-red veto → lock → 红码
    red = [e for e in art["entries"] if e["color"] == "red"]
    assert red and red[0]["clause"]
    assert red[0]["color_cn"] == "红码"


# -------------------------------------------------------------------- ledger


def test_ledger_statuses(proj_svc) -> None:
    store, projector, executor, svc = proj_svc
    seed_mixed(store, projector, executor)
    art = svc.generate_ledger(AS_OF, NOW)
    by_ref = {o["hazard_ref"]: o for o in art["orders"]}
    assert by_ref["hz-a"]["status"] == "closed"
    assert by_ref["hz-b"]["status"] == "overdue"  # due 2026-10-05 < as_of 2026-10-15
    assert art["closed_count"] == 1 and art["overdue_count"] == 1


# -------------------------------------------------------------- idempotency


def test_regeneration_is_idempotent(proj_svc) -> None:
    import json

    store, projector, executor, svc = proj_svc
    seed_mixed(store, projector, executor)
    first_paths = svc.regenerate_all(AS_OF, NOW)
    first = {k: json.loads(p.read_text()) for k, p in first_paths.items()}
    second_paths = svc.regenerate_all(AS_OF, NOW + timedelta(minutes=5))
    second = {k: json.loads(p.read_text()) for k, p in second_paths.items()}
    assert set(first) == set(second)
    for kind in first:
        assert strip_regenerated_at(first[kind]) == strip_regenerated_at(second[kind])
        assert first[kind]["stamp"]["regenerated_at"] != second[kind]["stamp"]["regenerated_at"]
