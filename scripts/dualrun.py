#!/usr/bin/env python3
"""Daily dual-run (shadow) comparison — W-009.

For a given day:
  1. read the new kernel's decision for every object (projector cache / fresh compute);
  2. load the legacy daily snapshot from the landing zone (ops-side export);
  3. compare levels, classify diffs (boundary jitter / explained / unexplained);
  4. write the daily report (markdown + JSON) and append the ruling-ledger entry;
  5. append a projection_only dual_run record to the log (audit trail).

Rules (verification area rule 2): explainable diffs → DECISIONS.md;
unexplainable → fix the function, never patch rules.

Usage:
    python3 scripts/dualrun.py --day 2026-10-15 [--db .tmp/riskcore.db]
        [--legacy-dir .tmp/legacy_snapshots] [--out outputs/dualrun]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for area in ("event-log", "risk-function", "executor", "verification"):
    p = ROOT / "areas" / area
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from eventlog import EventEnvelope, EventType, Provenance, SqliteStore  # noqa: E402
from eventlog.projector import SqliteProjector  # noqa: E402
from riskfn import load_policy  # noqa: E402
from duality import compare, level_for_r  # noqa: E402
from legacy_source import FileLandingLegacySource, LegacySnapshotError  # noqa: E402

TENANT = "t1"
RULINGS_FILE = ROOT / "DECISIONS.md"


def run(day: date, db: str, legacy_dir: str, out_dir: Path, tenant: str = TENANT) -> int:
    store = SqliteStore(db)
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, tenant)

    # 1. 新内核等级（以当日水位线全量投影为准）
    #    跳过系统审计对象（__ 前缀，如 dual_run/reconciliation 留痕）——它们不是业务对象
    as_of = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc)
    new_levels: dict[str, tuple[str, int | None]] = {}
    for oid in store.iter_objects(tenant):
        if oid.startswith("__"):
            continue
        out = projector.project_object(oid, as_of)
        new_levels[oid] = (level_for_r(out.decision.r_milli, out.decision.veto_active, policy.executor.thresholds),
                           out.decision.r_milli)

    # 2. legacy 快照
    legacy = FileLandingLegacySource(legacy_dir)
    try:
        legacy.fetch_levels(day)
    except LegacySnapshotError as exc:
        print(f"[dualrun] {exc}")
        print("[dualrun] 把现网 risk_code_assignments 每日导出放到该目录后重跑；")
        print("[dualrun] CSV 列：object_id,level（level ∈ blue|yellow|orange|red）")
        store.close()
        return 2

    # 3. 比对
    report = compare(day, new_levels, legacy, thresholds=policy.executor.thresholds)
    md = report.render_markdown(policy.policy_id)

    # 4. 落盘日报
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day.isoformat()}.md").write_text(md + "\n", encoding="utf-8")
    (out_dir / f"{day.isoformat()}.json").write_text(
        json.dumps(
            {
                "day": day.isoformat(),
                "total": report.total,
                "agreed": report.agreed,
                "agreement_rate_milli": report.agreement_rate_milli,
                "diffs": [
                    {
                        "object_id": d.object_id,
                        "new_level": d.new_level,
                        "legacy_level": d.legacy_level,
                        "new_r_milli": d.new_r_milli,
                        "classification": d.classification,
                        "note": d.note,
                    }
                    for d in report.diffs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 5. projection_only 留痕 + 裁决台账提示
    ev = EventEnvelope(
        event_id=__import__("uuid").uuid4(),
        tenant_id=tenant,
        trace_id=f"dualrun-{day.isoformat()}",
        object_id="__system__",
        object_type="enterprise",
        occurred_at=as_of,
        ingested_at=datetime.now(timezone.utc),
        type=EventType.PROJECTION_ONLY,
        payload={
            "material_kind": "dual_run",
            "data": {
                "day": day.isoformat(),
                "total": report.total,
                "agreed": report.agreed,
                "agreement_rate_milli": report.agreement_rate_milli,
                "unexplained": sum(1 for d in report.diffs if d.classification == "unexplained"),
            },
        },
        provenance=Provenance(source_system="dualrun"),
    )
    store.append(ev)

    unexplained = [d for d in report.diffs if d.classification == "unexplained"]
    print(f"[dualrun] {day}: 一致率 {report.agreement_rate_milli / 10:.1f}% "
          f"({report.agreed}/{report.total})，差异 {len(report.diffs)} 条，"
          f"不可解释 {len(unexplained)} 条")
    print(f"[dualrun] 日报: {out_dir / (day.isoformat() + '.md')}")
    if unexplained:
        print("[dualrun] 不可解释差异（修函数，不补规则）：")
        for d in unexplained:
            print(f"  - {d.object_id} new={d.new_level} legacy={d.legacy_level} ({d.note})")
    store.close()
    return 1 if unexplained else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", required=True, help="YYYY-MM-DD")
    parser.add_argument("--db", default=str(ROOT / ".tmp" / "riskcore.db"))
    parser.add_argument("--legacy-dir", default=str(ROOT / ".tmp" / "legacy_snapshots"))
    parser.add_argument("--out", default=str(ROOT / "outputs" / "dualrun"))
    args = parser.parse_args()
    raise SystemExit(run(date.fromisoformat(args.day), args.db, args.legacy_dir, Path(args.out)))


if __name__ == "__main__":
    main()
