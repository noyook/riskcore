"""Deterministic event factories for tests and the seeded stream generator.

Every timestamp is explicit — nothing here reads a wall clock. Factories are
shared by unit tests, property tests, golden fixtures and the projector tests.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from eventlog import EventEnvelope, EventType, ObjectType, Provenance

AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
TENANT = "t1"


def _env(
    *,
    object_id: str,
    event_type: EventType,
    payload: dict,
    occurred_at: datetime,
    object_type: ObjectType = ObjectType.VEHICLE,
    tenant_id: str = TENANT,
    event_id: UUID | None = None,
    ingested_at: datetime | None = None,
    source_system: str = "test-source",
    operator: str | None = None,
    trace_id: str | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or uuid4(),
        tenant_id=tenant_id,
        trace_id=trace_id or f"tr-{object_id}",
        object_id=object_id,
        object_type=object_type,
        occurred_at=occurred_at,
        ingested_at=ingested_at or occurred_at + timedelta(seconds=1),
        type=event_type,
        payload=payload,
        provenance=Provenance(source_system=source_system, operator=operator),
    )


def telemetry_event(
    object_id: str,
    occurred_at: datetime,
    *,
    continuous_driving_minutes: int = 0,
    speed_max_kmh: int = 60,
    device_id: str = "obd-1",
    **kw,
) -> EventEnvelope:
    payload = {
        "window_start": occurred_at.isoformat(),
        "window_end": (occurred_at + timedelta(minutes=10)).isoformat(),
        "speed_max_kmh": speed_max_kmh,
        "continuous_driving_minutes": continuous_driving_minutes,
        "device_id": device_id,
    }
    return _env(
        object_id=object_id,
        event_type=EventType.TELEMETRY,
        payload=payload,
        occurred_at=occurred_at,
        **kw,
    )


def violation_event(
    object_id: str,
    occurred_at: datetime,
    *,
    kind: str = "speeding",
    count: int = 1,
    duration_minutes: int = 10,
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.VIOLATION,
        payload={
            "violation_kind": kind,
            "count": count,
            "duration_minutes": duration_minutes,
            "detected_at": occurred_at.isoformat(),
        },
        occurred_at=occurred_at,
        **kw,
    )


def inspection_event(
    object_id: str,
    occurred_at: datetime,
    *,
    hazard_level: str = "general",
    location: str = " depot-A",
    hazard_ref: str | None = None,
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.INSPECTION,
        payload={
            "hazard_level": hazard_level,
            "location": location.strip(),
            "photo_refs": [],
            "inspected_at": occurred_at.isoformat(),
            "hazard_ref": hazard_ref,
        },
        occurred_at=occurred_at,
        **kw,
    )


def registry_event(
    object_id: str,
    occurred_at: datetime,
    *,
    registry_kind: str = "tank_inspection",
    expires_on: date | None = None,
    status: str = "valid",
    document_ref: str | None = None,
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.REGISTRY,
        payload={
            "registry_kind": registry_kind,
            "expires_on": (expires_on or date(2027, 6, 30)).isoformat(),
            "status": status,
            "document_ref": document_ref,
        },
        occurred_at=occurred_at,
        **kw,
    )


def work_order_created(
    object_id: str, occurred_at: datetime, *, hazard_ref: str = "hz-1", order_type: str = "rectify", due_date: date | None = None, **kw
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.WORK_ORDER_CREATED,
        payload={
            "hazard_ref": hazard_ref,
            "order_type": order_type,
            "due_date": (due_date or date(2026, 11, 1)).isoformat(),
        },
        occurred_at=occurred_at,
        **kw,
    )


def work_order_closed(
    object_id: str, occurred_at: datetime, *, closes_order_id: str = "hz-1", acceptor: str = "inspector-1", **kw
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.WORK_ORDER_CLOSED,
        payload={
            "closes_order_id": closes_order_id,
            "closed_at": occurred_at.isoformat(),
            "acceptor": acceptor,
        },
        occurred_at=occurred_at,
        **kw,
    )


def expert_assertion_event(
    object_id: str,
    occurred_at: datetime,
    *,
    L: int = 3,
    E: int = 3,
    C: int = 3,
    valid_until: date | None = None,
    hazard_source: str = "危险源",
    control_measures: str = "管控措施",
    operator: str = "expert-1",
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.EXPERT_ASSERTION,
        payload={
            "hazard_source": hazard_source,
            "control_measures": control_measures,
            "L": L,
            "E": E,
            "C": C,
            "valid_until": (valid_until or date(2027, 10, 15)).isoformat(),
        },
        occurred_at=occurred_at,
        object_type=ObjectType.DRIVER,
        operator=operator,
        source_system="annual-assessment",
        **kw,
    )


def correction_event(
    object_id: str,
    occurred_at: datetime,
    *,
    corrects_event_id: UUID,
    corrected_payload: dict,
    reason: str = "manual fix",
    operator: str = "auditor-1",
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.CORRECTION,
        payload={
            "corrects_event_id": str(corrects_event_id),
            "reason": reason,
            "corrected_payload": corrected_payload,
        },
        occurred_at=occurred_at,
        operator=operator,
        source_system="audit-desk",
        **kw,
    )


def manual_override_event(
    object_id: str,
    occurred_at: datetime,
    *,
    override_level: str = "restrict",
    reason: str = "manual review",
    approver: str = "supervisor-1",
    expires_at: datetime | None = None,
    operator: str = "dispatcher-1",
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.MANUAL_OVERRIDE,
        payload={
            "override_level": override_level,
            "reason": reason,
            "approver": approver,
            "expires_at": (expires_at or occurred_at + timedelta(hours=72)).isoformat(),
        },
        occurred_at=occurred_at,
        operator=operator,
        source_system="dispatch-desk",
        **kw,
    )


def projection_only_event(
    object_id: str,
    occurred_at: datetime,
    *,
    material_kind: str = "reconciliation_run",
    data: dict | None = None,
    **kw,
) -> EventEnvelope:
    return _env(
        object_id=object_id,
        event_type=EventType.PROJECTION_ONLY,
        payload={"material_kind": material_kind, "data": data or {}},
        occurred_at=occurred_at,
        **kw,
    )
