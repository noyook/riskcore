#!/usr/bin/env python3
"""Generate golden replay fixtures (fixed ids/timestamps — fully deterministic).

Covers: veto / mid-risk / clean / prior-only / correction-chain objects.
After editing, run `python3 -m verification.replay --record` to re-record
expectations (human review required — test-plan.md §1).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "areas" / "event-log"))
sys.path.insert(0, str(ROOT / "areas" / "verification"))

from factories import (  # noqa: E402
    correction_event,
    expert_assertion_event,
    inspection_event,
    registry_event,
    telemetry_event,
    violation_event,
    work_order_closed,
    work_order_created,
)

AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
EARLY = AS_OF - timedelta(days=2)
OUT = ROOT / "areas" / "verification" / "golden" / "fixtures.json"

# deterministic event ids
def eid(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def dump(ev) -> dict:
    d = ev.model_dump(mode="json")
    d["event_id"] = str(ev.event_id)
    return d


fixtures = []

# 1. veto：罐检逾期 + 超速 + 疲劳遥测
fixtures.append({
    "name": "veto_tank_overdue",
    "object_id": "g-veto-1",
    "as_of": AS_OF.isoformat(),
    "policy_id": "v1",
    "events": [
        dump(registry_event("g-veto-1", EARLY, registry_kind="tank_inspection",
                            expires_on=date(2026, 10, 1), status="expired", event_id=eid(1))),
        dump(violation_event("g-veto-1", EARLY, kind="speeding", count=3, event_id=eid(2))),
        dump(telemetry_event("g-veto-1", EARLY, continuous_driving_minutes=310,
                             speed_max_kmh=128, event_id=eid(3))),
    ],
})

# 2. mid：超速违法 + 疲劳遥测（进入预警/限单带）
fixtures.append({
    "name": "mid_risk",
    "object_id": "g-mid-1",
    "as_of": AS_OF.isoformat(),
    "policy_id": "v1",
    "events": [
        dump(violation_event("g-mid-1", EARLY, kind="speeding", count=3, event_id=eid(11))),
        dump(telemetry_event("g-mid-1", EARLY, continuous_driving_minutes=300,
                             speed_max_kmh=110, event_id=eid(12))),
        dump(telemetry_event("g-mid-1", EARLY, continuous_driving_minutes=260,
                             speed_max_kmh=95, event_id=eid(13))),
    ],
})

# 3. clean：良性遥测
fixtures.append({
    "name": "clean",
    "object_id": "g-clean-1",
    "as_of": AS_OF.isoformat(),
    "policy_id": "v1",
    "events": [
        dump(telemetry_event("g-clean-1", EARLY, continuous_driving_minutes=120,
                             speed_max_kmh=70, event_id=eid(21))),
    ],
})

# 4. prior-only：只有年度辨识，无传感器
fixtures.append({
    "name": "prior_only",
    "object_id": "g-prior-1",
    "as_of": AS_OF.isoformat(),
    "policy_id": "v1",
    "events": [
        dump(expert_assertion_event("g-prior-1", EARLY, L=6, E=5, C=6,
                                    valid_until=date(2027, 1, 1), event_id=eid(31))),
    ],
})

# 5. correction 链：超速 5 次被更正为 1 次
bad = violation_event("g-corr-1", EARLY, kind="speeding", count=5, event_id=eid(41))
fixtures.append({
    "name": "correction_chain",
    "object_id": "g-corr-1",
    "as_of": AS_OF.isoformat(),
    "policy_id": "v1",
    "events": [
        dump(bad),
        dump(correction_event("g-corr-1", EARLY + timedelta(minutes=5),
                              corrects_event_id=bad.event_id,
                              corrected_payload={
                                  "violation_kind": "speeding", "count": 1,
                                  "duration_minutes": 10, "detected_at": EARLY.isoformat(),
                              },
                              event_id=eid(42))),
    ],
})

# 6. 隐患闭环：重大隐患 → 工单 → 闭环（无 veto）
insp = inspection_event("g-closed-1", EARLY, hazard_level="major", hazard_ref="hz-g1", event_id=eid(51))
fixtures.append({
    "name": "hazard_closed",
    "object_id": "g-closed-1",
    "as_of": AS_OF.isoformat(),
    "policy_id": "v1",
    "events": [
        dump(insp),
        dump(work_order_created("g-closed-1", EARLY, hazard_ref="hz-g1",
                                due_date=date(2026, 10, 20), event_id=eid(52))),
        dump(work_order_closed("g-closed-1", EARLY + timedelta(hours=3),
                               closes_order_id="hz-g1", event_id=eid(53))),
    ],
})

OUT.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"wrote {OUT} ({len(fixtures)} fixtures)")
