#!/usr/bin/env python3
"""Simulate a multi-day dual-run to prove the W-009 pipeline end-to-end.

Steps:
  1. seed a synthetic fleet (varied risk profiles);
  2. compute the NEW kernel's level per object (projector);
  3. synthesize the legacy daily snapshot: identical levels, except three
     crafted cases — new_higher / boundary / new_lower — so every diff class
     in the comparator is exercised;
  4. run scripts/dualrun.py for 5 consecutive days.

THIS IS A PIPELINE REHEARSAL, NOT REAL DATA. The real 14-day run starts when
ops exports risk_code_assignments daily into the landing zone (STATUS Q3).
"""
from __future__ import annotations

import csv
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

DB = ROOT / ".tmp" / "dualrun.db"
LEGACY_DIR = ROOT / ".tmp" / "legacy_snapshots"
OUT = ROOT / "outputs" / "dualrun"
DAYS = [date(2026, 10, 12) + timedelta(days=i) for i in range(5)]
LEVELS = ("blue", "yellow", "orange", "red")


def seed_events() -> dict[str, str]:
    sys.path.insert(0, str(ROOT / "areas" / "event-log"))
    sys.path.insert(0, str(ROOT / "areas" / "risk-function"))
    sys.path.insert(0, str(ROOT / "areas" / "verification"))
    from eventlog import SqliteStore
    from eventlog.projector import SqliteProjector
    from riskfn import load_policy
    from factories import AS_OF, TENANT, inspection_event, registry_event, telemetry_event, violation_event, work_order_created
    from duality import level_for_r

    if DB.exists():
        DB.unlink()
    store = SqliteStore(DB)
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, TENANT)
    early = AS_OF - timedelta(days=3)

    def many(oid, n, factory, **kw):
        for i in range(n):
            store.append(factory(oid, early + timedelta(minutes=i), **kw))

    # 红：罐检逾期 veto
    store.append(registry_event("veh-101", early, registry_kind="tank_inspection",
                                expires_on=date(2026, 10, 5), status="expired"))
    # veh-102 → 黄（违法+遥测+隐患+逾期工单全中）
    many("veh-102", 5, violation_event, kind="speeding", count=1)
    many("veh-102", 3, violation_event, kind="fatigue", count=1)
    many("veh-102", 4, telemetry_event, continuous_driving_minutes=300, speed_max_kmh=120)
    many("veh-102", 8, telemetry_event, continuous_driving_minutes=60, speed_max_kmh=125)
    many("veh-102", 5, inspection_event, hazard_level="general", hazard_ref="hz-a")
    many("veh-102", 3, work_order_created, hazard_ref="hz-b", due_date=date(2026, 10, 1))
    # veh-103 → 橙（黄档全中 + 证照/罐检临期）
    many("veh-103", 5, violation_event, kind="speeding", count=1)
    many("veh-103", 3, violation_event, kind="fatigue", count=1)
    many("veh-103", 4, telemetry_event, continuous_driving_minutes=300, speed_max_kmh=120)
    many("veh-103", 8, telemetry_event, continuous_driving_minutes=60, speed_max_kmh=125)
    many("veh-103", 5, inspection_event, hazard_level="general", hazard_ref="hz-c")
    many("veh-103", 3, work_order_created, hazard_ref="hz-d", due_date=date(2026, 10, 1))
    store.append(registry_event("veh-103", early, registry_kind="license",
                                expires_on=date(2026, 10, 25), status="valid"))
    store.append(registry_event("veh-103", early + timedelta(minutes=1), registry_kind="tank_inspection",
                                expires_on=date(2026, 10, 25), status="valid"))
    # 干净对象 ×3
    for oid in ("veh-104", "veh-105", "veh-108"):
        store.append(telemetry_event(oid, early, continuous_driving_minutes=100, speed_max_kmh=60))
    # 证照临期（低分但新内核可见）
    store.append(registry_event("veh-106", early, registry_kind="license",
                                expires_on=date(2026, 11, 10), status="valid"))
    # veh-107 → 边界对象：r 贴近 400 线（用于 boundary 抖动演示）
    many("veh-107", 4, violation_event, kind="speeding", count=1)
    many("veh-107", 2, violation_event, kind="fatigue", count=1)
    many("veh-107", 4, telemetry_event, continuous_driving_minutes=300, speed_max_kmh=120)
    many("veh-107", 4, telemetry_event, continuous_driving_minutes=60, speed_max_kmh=125)
    many("veh-107", 5, inspection_event, hazard_level="general", hazard_ref="hz-e")
    many("veh-107", 3, work_order_created, hazard_ref="hz-f", due_date=date(2026, 10, 1))
    # new_lower 对象：legacy 认为更危险
    store.append(violation_event("veh-109", early, kind="speeding", count=1))

    levels: dict[str, str] = {}
    for oid in ("veh-101", "veh-102", "veh-103", "veh-104", "veh-105",
                "veh-106", "veh-107", "veh-108", "veh-109"):
        out = projector.project_object(oid, AS_OF)
        levels[oid] = level_for_r(out.decision.r_milli, out.decision.veto_active,
                                  policy.executor.thresholds)
        print(f"  {oid}: r={out.decision.r_milli} veto={out.decision.veto_active} → {levels[oid]}")
    store.close()
    return levels


def write_legacy_snapshots(new_levels: dict[str, str]) -> None:
    LEGACY_DIR.mkdir(parents=True, exist_ok=True)
    # 选一个 r 贴近阈值的对象作 boundary 案例：veh-107（轻度堆叠）
    boundary_obj = "veh-107"
    legacy = dict(new_levels)
    # new_higher：veh-103 legacy 低一档（新内核更严）
    if LEVELS.index(legacy.get("veh-103", "blue")) > 0:
        legacy["veh-103"] = LEVELS[LEVELS.index(legacy["veh-103"]) - 1]
    # boundary：veh-107 legacy 低一档，且 r 贴近阈值 → 判定为口径抖动
    if LEVELS.index(legacy.get(boundary_obj, "blue")) > 0:
        legacy[boundary_obj] = LEVELS[LEVELS.index(legacy[boundary_obj]) - 1]
    # new_lower：veh-109 legacy 高一档（新内核更松，需裁决）
    if LEVELS.index(legacy.get("veh-109", "blue")) < 3:
        legacy["veh-109"] = LEVELS[LEVELS.index(legacy["veh-109"]) + 1]

    for day in DAYS:
        with (LEGACY_DIR / f"{day.isoformat()}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["object_id", "level"])
            for oid, level in legacy.items():
                w.writerow([oid, level])
    print(f"  legacy 快照已写 {len(DAYS)} 天（crafted diffs: veh-103 higher, {boundary_obj} boundary, veh-109 lower）")


def main() -> None:
    print("== 1. 种子车队 + 新内核等级 ==")
    levels = seed_events()
    print("== 2. 合成 legacy 每日快照 ==")
    write_legacy_snapshots(levels)
    print("== 3. 逐日双跑 ==")
    for day in DAYS:
        subprocess.run(
            [PY, str(ROOT / "scripts" / "dualrun.py"),
             "--day", day.isoformat(), "--db", str(DB),
             "--legacy-dir", str(LEGACY_DIR), "--out", str(OUT)],
            check=False,
        )
    print("== 4. 日报清单 ==")
    for p in sorted(OUT.glob("*.md")):
        print(" ", p.relative_to(ROOT))


if __name__ == "__main__":
    main()
