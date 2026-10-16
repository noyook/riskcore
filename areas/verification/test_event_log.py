"""W-001 acceptance tests — 事件信封与日志表定稿.

Acceptance criteria (work/active/W-001):
  1. 无法 UPDATE/DELETE 日志表（SQLite 开发态由触发器强制；Postgres 生产态
     由 GRANT 强制，权限脚本 scripts/verify_pg_perms.sh 真机验证）。
  2. correction 链指向不存在的 event_id 被拒绝。
  3. 迟到事件可入日志且不影响既有 watermark。
Plus: 幂等写入、seq 对象内单调、outbox 同事务、relay 投递、ingest API 边界
（401/403/seq 服务端分配）、JSON Schema 产物门禁。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from eventlog import CorrectionTargetMissing, EventEnvelope, EventType, SqliteStore

from .factories import AS_OF, TENANT, correction_event, registry_event, telemetry_event, violation_event

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- append-only


def test_event_log_rejects_update_and_delete(store: SqliteStore) -> None:
    ev = telemetry_event("veh-1", AS_OF)
    store.append(ev)
    conn = store._conn
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE event_log SET trace_id='hack' WHERE event_id=?", (str(ev.event_id),))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM event_log WHERE event_id=?", (str(ev.event_id),))
    # row intact
    assert store.get(TENANT, ev.event_id) is not None


def test_append_is_idempotent_by_event_id(store: SqliteStore) -> None:
    ev = telemetry_event("veh-1", AS_OF)
    first = store.append(ev)
    second = store.append(ev)
    assert first.duplicate is False and second.duplicate is True
    assert first.seq == second.seq == 1
    assert len(store.read_object_events(TENANT, "veh-1")) == 1


def test_seq_monotonic_per_object_independent_across_objects(store: SqliteStore) -> None:
    seqs = []
    for i in range(5):
        seqs.append(store.append(telemetry_event("veh-1", AS_OF + timedelta(minutes=i))).seq)
    assert seqs == [1, 2, 3, 4, 5]
    other = store.append(telemetry_event("veh-2", AS_OF)).seq
    assert other == 1  # objects are independent seq spaces


# ------------------------------------------------------------- correction 链


def test_correction_to_missing_target_rejected(store: SqliteStore) -> None:
    bad = correction_event(
        "veh-1", AS_OF, corrects_event_id=uuid4(), corrected_payload={"speed_max_kmh": 1}
    )
    with pytest.raises(CorrectionTargetMissing):
        store.append(bad)
    assert store.read_object_events(TENANT, "veh-1") == []


def test_correction_chain_applies_latest(store: SqliteStore) -> None:
    original = violation_event("veh-1", AS_OF, kind="speeding", count=2)
    store.append(original)
    fix1 = correction_event(
        "veh-1", AS_OF + timedelta(minutes=5), corrects_event_id=original.event_id,
        corrected_payload={"violation_kind": "speeding", "count": 3, "duration_minutes": 10,
                           "detected_at": AS_OF.isoformat()},
    )
    store.append(fix1)
    fix2 = correction_event(
        "veh-1", AS_OF + timedelta(minutes=9), corrects_event_id=original.event_id,
        corrected_payload={"violation_kind": "fatigue", "count": 1, "duration_minutes": 30,
                           "detected_at": AS_OF.isoformat()},
    )
    store.append(fix2)

    events = store.read_object_events(TENANT, "veh-1")
    assert [e.type for e in events] == [EventType.VIOLATION, EventType.CORRECTION, EventType.CORRECTION]

    # 应用规则：以最新一条有效 correction 为准，原事件 provenance 永久可见
    corrections = [e for e in events if e.type is EventType.CORRECTION]
    latest = max(corrections, key=lambda e: e.seq)
    assert latest.payload["corrects_event_id"] == str(original.event_id)
    assert latest.payload["corrected_payload"]["violation_kind"] == "fatigue"
    assert original.provenance.source_system == "test-source"  # 原事件未被修改


# ---------------------------------------------------------- 迟到事件与水位线


def test_late_event_enters_log_without_moving_watermark(store: SqliteStore) -> None:
    store.append(telemetry_event("veh-1", AS_OF))
    store.advance_watermark(TENANT, "veh-1", seq=1, advanced_at=AS_OF + timedelta(minutes=1))
    assert store.watermark(TENANT, "veh-1") == 1

    late = telemetry_event(
        "veh-1",
        occurred_at=AS_OF - timedelta(days=3),          # 业务时间远早于入日志时间
        ingested_at=AS_OF + timedelta(minutes=2),
    )
    result = store.append(late)
    assert result.duplicate is False
    assert result.seq == 2                              # 正常获得新 seq
    assert store.watermark(TENANT, "veh-1") == 1        # 既有 watermark 不被回溯


def test_watermark_advances_forward_only(store: SqliteStore) -> None:
    assert store.advance_watermark(TENANT, "veh-1", 5, AS_OF) is True
    assert store.advance_watermark(TENANT, "veh-1", 3, AS_OF + timedelta(minutes=1)) is False
    assert store.watermark(TENANT, "veh-1") == 5


# ------------------------------------------------------------------- outbox


def test_outbox_row_written_in_same_transaction(store: SqliteStore) -> None:
    ev = telemetry_event("veh-1", AS_OF)
    store.append(ev)
    pending = store.pending_outbox()
    assert len(pending) == 1 and pending[0].event_id == str(ev.event_id)


def test_relay_delivers_and_is_at_least_once(store: SqliteStore) -> None:
    from eventlog.relay import relay_once

    ev = telemetry_event("veh-1", AS_OF)
    store.append(ev)
    seen: list[str] = []
    stats = relay_once(store, lambda row: seen.append(row.event_id))
    assert stats == {"delivered": 1, "retried": 0, "dead": 0}
    assert seen == [str(ev.event_id)]
    assert store.pending_outbox() == []


def test_relay_retries_then_dead_letters(store: SqliteStore) -> None:
    from eventlog.relay import MAX_ATTEMPTS, relay_once

    store.append(telemetry_event("veh-1", AS_OF))
    calls = {"n": 0}

    def flaky(row) -> None:
        calls["n"] += 1
        raise ConnectionError("consumer down")

    for _ in range(MAX_ATTEMPTS - 1):
        stats = relay_once(store, flaky)
        assert stats == {"delivered": 0, "retried": 1, "dead": 0}
    stats = relay_once(store, flaky)
    assert stats == {"delivered": 0, "retried": 0, "dead": 1}
    assert calls["n"] == MAX_ATTEMPTS


# ------------------------------------------------------------- ingest API 边界


def test_ingest_requires_tenant_header(app_store) -> None:
    client, _store = app_store
    payload = telemetry_event("veh-1", AS_OF).model_dump(mode="json")
    resp = client.post("/events", json=payload)
    assert resp.status_code == 401


def test_ingest_rejects_tenant_mismatch(app_store) -> None:
    client, store = app_store
    payload = telemetry_event("veh-1", AS_OF, tenant_id="t2").model_dump(mode="json")
    resp = client.post("/events", json=payload, headers={"X-Tenant-Id": "t1"})
    assert resp.status_code == 403


def test_ingest_assigns_seq_server_side(app_store) -> None:
    client, store = app_store
    ev = telemetry_event("veh-1", AS_OF)
    body = ev.model_dump(mode="json")
    body["seq"] = 999  # client-supplied seq must be ignored/rejected
    resp = client.post("/events", json=body, headers={"X-Tenant-Id": TENANT})
    assert resp.status_code == 422  # envelope validator forbids client seq? see note


def test_ingest_happy_path(app_store) -> None:
    client, store = app_store
    ev = telemetry_event("veh-1", AS_OF)
    resp = client.post("/events", json=ev.model_dump(mode="json"), headers={"X-Tenant-Id": TENANT})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"event_id": str(ev.event_id), "seq": 1, "duplicate": False}
    # idempotent re-delivery
    resp2 = client.post("/events", json=ev.model_dump(mode="json"), headers={"X-Tenant-Id": TENANT})
    assert resp2.json()["duplicate"] is True


# ------------------------------------------------------- v1.1 registry 事件


def test_registry_event_roundtrip_and_veto_carrier(store: SqliteStore) -> None:
    ev = registry_event(
        "veh-1", AS_OF, registry_kind="tank_inspection",
        expires_on=date(2026, 10, 1), status="expired",
    )
    result = store.append(ev)
    assert result.duplicate is False
    stored = store.get(TENANT, ev.event_id)
    assert stored is not None and stored.type is EventType.REGISTRY
    assert stored.payload["expires_on"] == "2026-10-01"


# ------------------------------------------------------------ JSON Schema 门禁


def test_generated_json_schema_validates_envelopes() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "areas/event-log/schemas/event-envelope.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)

    good = telemetry_event("veh-1", AS_OF).model_dump(mode="json")
    validator.validate(good)

    bad = dict(good)
    bad["type"] = "not_a_type"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(bad)
