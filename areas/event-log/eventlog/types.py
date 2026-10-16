"""Event / object type dictionaries — the single source of truth.

Mirrors areas/event-log/event-schema.md v1. Any change to these enums is a
schema change: it must pass areas/verification/ before landing (AGENTS.md 四-5).
"""
from __future__ import annotations

from enum import Enum


class ObjectType(str, Enum):
    DRIVER = "driver"
    VEHICLE = "vehicle"
    ROUTE = "route"
    CARGO = "cargo"
    TRANSPORT_TASK = "transport_task"
    ENTERPRISE = "enterprise"


class EventType(str, Enum):
    TELEMETRY = "telemetry"
    VIOLATION = "violation"
    INSPECTION = "inspection"
    REGISTRY = "registry"  # v1.1 增补：监管期限类事实（证照/罐检/保险有效期），加权参与
    WORK_ORDER_CREATED = "work_order_created"
    WORK_ORDER_CLOSED = "work_order_closed"
    EXPERT_ASSERTION = "expert_assertion"
    CORRECTION = "correction"
    MANUAL_OVERRIDE = "manual_override"
    PROJECTION_ONLY = "projection_only"


#: Events whose provenance.operator is mandatory (human-initiated, D-009/D-010).
HUMAN_EVENT_TYPES = frozenset(
    {
        EventType.CORRECTION,
        EventType.MANUAL_OVERRIDE,
        EventType.EXPERT_ASSERTION,
    }
)

#: Events that participate in risk weighting. projection_only never does
#: (event-log area rule 5).
WEIGHTED_EVENT_TYPES = frozenset(
    {
        EventType.TELEMETRY,
        EventType.VIOLATION,
        EventType.INSPECTION,
        EventType.REGISTRY,
        EventType.WORK_ORDER_CREATED,
        EventType.WORK_ORDER_CLOSED,
        EventType.EXPERT_ASSERTION,
        EventType.MANUAL_OVERRIDE,
    }
)

OBJECT_TYPE_VALUES = tuple(o.value for o in ObjectType)
EVENT_TYPE_VALUES = tuple(e.value for e in EventType)
