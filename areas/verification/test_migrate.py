"""W-002 migration tests — 解析 / 幂等灌入 / 对账 / correction 比例。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from eventlog import EventType, SqliteStore
from eventlog.migrate import (
    AssertionDraft,
    VEHICLE_HAZARD_TEMPLATE,
    batch_correction_count,
    ingest_drafts,
    parse_tsv,
)
from eventlog.projector import SqliteProjector
from riskfn import load_policy

from .factories import AS_OF, TENANT

EARLY = AS_OF - timedelta(days=2)


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "migrate.db")
    yield s
    s.close()


def draft(object_id: str = "veh-001", hazard: str = "罐体阀门密封老化", **kw) -> AssertionDraft:
    base = dict(
        object_id=object_id, object_type="vehicle", hazard_source=hazard,
        L=3, E=4, C=7, control_measures="定期更换密封件",
        valid_until=date(2027, 9, 30), assessed_at=date(2026, 10, 1),
        operator="安全员-001", item_code="1.1",
    )
    base.update(kw)
    return AssertionDraft(**base)


# ------------------------------------------------------------------ template


def test_template_has_10_items() -> None:
    assert len(VEHICLE_HAZARD_TEMPLATE) == 10
    codes = [i.code for i in VEHICLE_HAZARD_TEMPLATE]
    assert codes == [f"1.{n}" for n in range(1, 11)]


# --------------------------------------------------------------------- parse


def test_parse_tsv_happy_path() -> None:
    text = (
        "# 注释行\n"
        "veh-001\t1.1\t罐体阀门密封老化\t3\t4\t7\t定期更换\t2027-09-30\n"
        "veh-002, 1.3, 驾驶员疲劳驾驶, 4, 5, 8\n"
    )
    result = parse_tsv(text, operator="安全员-001", assessed_at=date(2026, 10, 1),
                       valid_until=date(2027, 9, 30))
    assert result.ok and len(result.drafts) == 2
    assert result.drafts[0].item_code == "1.1"
    assert result.drafts[0].L * result.drafts[0].E * result.drafts[0].C == 84
    assert result.drafts[1].object_id == "veh-002"  # 逗号分隔兼容
    assert result.drafts[1].control_measures == "见纸质记录表"  # 缺省措施


def test_parse_tsv_collects_errors_without_stopping() -> None:
    text = "veh-001\t1.1\t罐体\t3\t4\t7\nveh-002\t坏行\nveh-003\t1.3\t超速\t99\t1\t1\n"
    result = parse_tsv(text, operator="op", assessed_at=date(2026, 10, 1),
                       valid_until=date(2027, 9, 30))
    assert not result.ok
    assert len(result.drafts) == 1 and len(result.errors) == 2


# -------------------------------------------------------------------- ingest


def test_ingest_creates_events_with_migration_provenance(store) -> None:
    report = ingest_drafts(store, [draft(), draft(object_id="veh-002", hazard="驾驶员疲劳驾驶")],
                           tenant_id=TENANT, operator="安全员-001", batch_id="b1")
    s = report.summary()
    assert s["submitted_rows"] == 2 and s["created_events"] == 2
    assert s["reconciled"] is True
    ev = store.get(TENANT, report.rows[0].event_id)
    assert ev is not None
    assert ev.type is EventType.EXPERT_ASSERTION
    assert ev.provenance.source_system == "historical-migration"
    assert ev.provenance.operator == "安全员-001"
    assert ev.trace_id == "migrate-b1"


def test_reingest_is_idempotent(store) -> None:
    first = ingest_drafts(store, [draft()], tenant_id=TENANT, operator="op", batch_id="b1")
    second = ingest_drafts(store, [draft()], tenant_id=TENANT, operator="op", batch_id="b2")
    assert first.summary()["created_events"] == 1
    assert second.summary()["duplicate_rows"] == 1
    assert second.summary()["created_events"] == 0
    assert second.summary()["reconciled"] is True
    assert len(store.read_object_events(TENANT, "veh-001")) == 1  # 不双计


def test_report_correction_ratio(store) -> None:
    report = ingest_drafts(store, [draft()], tenant_id=TENANT, operator="op", batch_id="b9")
    row = report.rows[0]
    # 人工修正：correction 事件（原事件不动）
    from eventlog.envelope import EventEnvelope, Provenance
    from uuid import uuid4

    ev = EventEnvelope(
        event_id=uuid4(), tenant_id=TENANT, trace_id="migrate-b9", object_id="veh-001",
        object_type="vehicle", occurred_at=AS_OF, ingested_at=AS_OF, type="correction",
        payload={"corrects_event_id": row.event_id, "reason": "字迹核对",
                 "corrected_payload": {"hazard_source": "罐体阀门密封老化", "control_measures": "x",
                                       "L": 2, "E": 3, "C": 5, "valid_until": "2027-09-30"}},
        provenance=Provenance(source_system="historical-migration", operator="auditor"),
    )
    store.append(ev)
    assert batch_correction_count(store, TENANT, "b9") == 1
    report.corrections = 1
    assert report.correction_ratio_milli == 1000  # 1/1


# ------------------------------------------------------- projector 消费（端到端）


def test_migrated_assertion_feeds_prior_injection(store) -> None:
    ingest_drafts(store, [draft(object_id="drv-001", hazard="危化品装卸区车辆伤害", L=6, E=5, C=6)],
                  tenant_id=TENANT, operator="op", batch_id="b1")
    projector = SqliteProjector(store, load_policy("v1"), TENANT)
    out = projector.project_object("drv-001", AS_OF)
    assert out.decision.prior_injected is True
    prior = out.snapshot.facts["lecd_prior"]
    assert prior.prior_r_milli == 180  # D=6*5*6
    # 任一纸质条目可按 object_id 检索到（W-002 验收 1）
    events = store.read_object_events(TENANT, "drv-001")
    assert len(events) == 1 and events[0].payload["hazard_source"] == "危化品装卸区车辆伤害"
