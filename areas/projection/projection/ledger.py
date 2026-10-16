"""双重预防闭环台账 (projections.md §双重预防闭环台账).

Source: work_order event flow + closed state. Generator over the log; the PG
DDL also ships an equivalent SQL view for direct audit queries.
"""
from __future__ import annotations

from datetime import datetime

from eventlog import EventEnvelope, EventType

from riskfn import FUNCTION_VERSION


def build_ledger(
    events: list[EventEnvelope], policy_id: str, as_of: datetime, regenerated_at: datetime
) -> dict:
    created: dict[str, dict] = {}
    rows = []
    for ev in sorted(events, key=lambda e: e.seq):
        if ev.type is EventType.WORK_ORDER_CREATED:
            ref = str(ev.payload["hazard_ref"])
            created[ref] = {
                "hazard_ref": ref,
                "object_id": ev.object_id,
                "order_type": ev.payload["order_type"],
                "due_date": str(ev.payload["due_date"]),
                "created_event": str(ev.event_id),
                "created_at": ev.occurred_at.isoformat(),
                "status": "open",
                "closed_event": None,
                "closed_at": None,
                "acceptor": None,
            }
        elif ev.type is EventType.WORK_ORDER_CLOSED:
            ref = str(ev.payload["closes_order_id"])
            row = created.get(ref)
            if row is not None and row["status"] == "open":
                row["status"] = "closed"
                row["closed_event"] = str(ev.event_id)
                row["closed_at"] = ev.occurred_at.isoformat()
                row["acceptor"] = ev.payload["acceptor"]
    for row in created.values():
        if row["status"] == "open" and row["due_date"] < as_of.date().isoformat():
            row["status"] = "overdue"
        rows.append(row)
    rows.sort(key=lambda r: r["hazard_ref"])
    return {
        "artifact": "dual_prevention_ledger",
        "stamp": {
            "function_version": FUNCTION_VERSION,
            "policy_id": policy_id,
            "as_of": as_of.isoformat(),
            "regenerated_at": regenerated_at.isoformat(),
        },
        "as_of": as_of.isoformat(),
        "open_count": sum(1 for r in rows if r["status"] == "open"),
        "overdue_count": sum(1 for r in rows if r["status"] == "overdue"),
        "closed_count": sum(1 for r in rows if r["status"] == "closed"),
        "orders": rows,
    }
