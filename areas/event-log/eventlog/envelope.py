"""Event envelope — executable contract for the fact log (event-schema.md v1).

Rules enforced here (event-log area rules 1-5):
- envelope fields are closed (extra="forbid"): no undeclared fields, ever;
- seq is NEVER client-supplied: ingest payloads carry seq=None, the store
  assigns it from the per-object counter (rule 3);
- human-initiated events (correction / manual_override / expert_assertion)
  must carry provenance.operator;
- payloads are validated against per-type models (incl. correction and
  projection_only);
- all timestamps are normalized to UTC so stored bytes are replay-stable.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import EVENT_TYPE_VALUES, HUMAN_EVENT_TYPES, OBJECT_TYPE_VALUES, EventType, ObjectType

__all__ = [
    "EventEnvelope",
    "StoredEvent",
    "Provenance",
    "parse_payload",
    "PAYLOAD_MODELS",
]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# payload models (per event type)
# --------------------------------------------------------------------------


class TelemetryPayload(_Base):
    window_start: datetime
    window_end: datetime
    speed_max_kmh: int = Field(ge=0, le=400)
    continuous_driving_minutes: int = Field(ge=0, le=24 * 60)
    tire_pressure_min_kpa: int | None = Field(default=None, ge=0)
    device_id: str


class ViolationPayload(_Base):
    violation_kind: Literal["speeding", "fatigue", "overload", "illegal_parking", "other"]
    count: int = Field(ge=1)
    duration_minutes: int = Field(ge=0)
    detected_at: datetime


class InspectionPayload(_Base):
    hazard_level: Literal["major", "general", "minor"]
    location: str
    photo_refs: list[str] = Field(default_factory=list)
    inspected_at: datetime
    hazard_ref: str | None = None  # 与 work_order 事件关联的隐患编号（开环/闭环配对键）


class RegistryPayload(_Base):
    """监管期限类事实（v1.1 增补）：证照/罐检/保险有效期。

    来源：车务/证照注册系统（source_system=vehicle-registry）。投影器按
    registry_kind 取最新一条派生 days_remaining 事实，喂给 veto 判定与
    单因子项；无事件 = 无数据（missing），不臆造。
    """

    registry_kind: Literal["license", "tank_inspection", "insurance", "road_worthiness"]
    expires_on: date
    status: Literal["valid", "expired", "revoked"]
    document_ref: str | None = None


class WorkOrderCreatedPayload(_Base):
    hazard_ref: str
    order_type: str
    due_date: date


class WorkOrderClosedPayload(_Base):
    closes_order_id: str
    closed_at: datetime
    acceptor: str


class ExpertAssertionPayload(_Base):
    """原 LEC-D 档案（D-010：年度辨识 = expert_assertion 事件）."""

    hazard_source: str
    control_measures: str
    L: int = Field(ge=1, le=10)  # 事故发生的可能性
    E: int = Field(ge=1, le=10)  # 暴露于危险环境的频繁程度
    C: int = Field(ge=1, le=10)  # 发生事故产生的后果严重程度
    valid_until: date

    @property
    def d_score(self) -> int:
        """LEC-D 风险值 D = L×E×C ∈ [1, 1000]，与 r_milli 同量纲，可直接作先验。"""
        return self.L * self.E * self.C


class CorrectionPayload(_Base):
    corrects_event_id: UUID
    reason: str = Field(min_length=1)
    corrected_payload: dict[str, Any]


class ManualOverridePayload(_Base):
    """人工覆写（D-009：一等事件，最高权重，带审批人与有效期，到期自动失效）."""

    override_level: Literal["alert", "restrict", "lock", "clear"]
    reason: str = Field(min_length=1)
    approver: str = Field(min_length=1)
    expires_at: datetime


class ProjectionOnlyPayload(_Base):
    """监管报告素材；绝不参与 risk 加权（event-log area rule 5）。"""

    material_kind: str = "unspecified"
    data: dict[str, Any] = Field(default_factory=dict)


PAYLOAD_MODELS = {
    EventType.TELEMETRY: TelemetryPayload,
    EventType.VIOLATION: ViolationPayload,
    EventType.INSPECTION: InspectionPayload,
    EventType.REGISTRY: RegistryPayload,
    EventType.WORK_ORDER_CREATED: WorkOrderCreatedPayload,
    EventType.WORK_ORDER_CLOSED: WorkOrderClosedPayload,
    EventType.EXPERT_ASSERTION: ExpertAssertionPayload,
    EventType.CORRECTION: CorrectionPayload,
    EventType.MANUAL_OVERRIDE: ManualOverridePayload,
    EventType.PROJECTION_ONLY: ProjectionOnlyPayload,
}


def parse_payload(event_type: EventType, payload: dict[str, Any]) -> BaseModel:
    """Typed access to a validated payload (raises on mismatch)."""
    return PAYLOAD_MODELS[event_type].model_validate(payload)


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------


class Provenance(_Base):
    source_system: str = Field(min_length=1)
    operator: str | None = None  # mandatory for HUMAN_EVENT_TYPES


class EventEnvelope(_Base):
    """唯一写入源协议（event-schema.md §信封结构）.

    Ingest form: seq is None (server-assigned). Stored form: StoredEvent.
    """

    event_id: UUID
    tenant_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    object_type: ObjectType
    seq: int | None = Field(default=None, ge=1)  # server-assigned, never client-supplied
    occurred_at: datetime
    ingested_at: datetime
    type: EventType
    payload: dict[str, Any]
    provenance: Provenance
    schema_version: str = "1"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "EventEnvelope":
        if self.type in HUMAN_EVENT_TYPES and not self.provenance.operator:
            raise ValueError(
                f"provenance.operator is mandatory for human-initiated event type '{self.type.value}'"
            )
        # Strict payload validation against the per-type model.
        parse_payload(self.type, self.payload)
        # Normalize timestamps to UTC for replay-stable storage bytes.
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))
        object.__setattr__(self, "ingested_at", self.ingested_at.astimezone(timezone.utc))
        return self


class StoredEvent(EventEnvelope):
    """An envelope as read back from the log: seq always present."""

    seq: int = Field(ge=1)

    def canonical_json(self) -> str:
        """Canonical serialization used for golden-replay byte comparison."""
        return self.model_dump_json(indent=None, exclude_none=False)  # type: ignore[return-value]


def validate_known_values() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (object_type_values, event_type_values) for DDL generation."""
    return OBJECT_TYPE_VALUES, EVENT_TYPE_VALUES
