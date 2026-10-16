"""Four-color risk distribution (projections.md §四色映射).

Thresholds come from policy.executor (alert/restrict/lock); veto_active forces
red. The mapping is data, not code — a regulatory realignment only changes the
policy or this table's source, never the kernel.
"""
from __future__ import annotations

from datetime import datetime

from riskfn import PolicyDocument

COLOR_CN = {"red": "红", "orange": "橙", "yellow": "黄", "blue": "蓝"}
LEVEL_CN = {"red": "公司级", "orange": "部门级", "yellow": "车间级", "blue": "岗位级"}


def color_for_r(r_milli: int, veto_active: bool, policy: PolicyDocument) -> str:
    if veto_active:
        return "red"
    th = policy.executor.thresholds
    if r_milli >= th["lock"]:
        return "red"
    if r_milli >= th["restrict"]:
        return "orange"
    if r_milli >= th["alert"]:
        return "yellow"
    return "blue"


def build_fourcolor(rows: list[dict], policy: PolicyDocument, as_of: datetime, regenerated_at: datetime) -> dict:
    """rows: [{object_id, object_type, r_milli, veto_active}] from decision cache."""
    counts = {"red": 0, "orange": 0, "yellow": 0, "blue": 0}
    entities = []
    for row in sorted(rows, key=lambda r: r["object_id"]):
        color = color_for_r(row["r_milli"], bool(row["veto_active"]), policy)
        counts[color] += 1
        entities.append(
            {
                "object_id": row["object_id"],
                "object_type": row.get("object_type", ""),
                "r_milli": row["r_milli"],
                "color": color,
                "color_cn": COLOR_CN[color],
                "control_level": LEVEL_CN[color],
            }
        )
    return {
        "artifact": "four_color_distribution",
        "stamp": {
            "function_version": __import__("riskfn").FUNCTION_VERSION,
            "policy_id": policy.policy_id,
            "as_of": as_of.isoformat(),
            "regenerated_at": regenerated_at.isoformat(),
        },
        "as_of": as_of.isoformat(),
        "counts": counts,
        "counts_cn": {COLOR_CN[k]: v for k, v in counts.items()},
        "total": sum(counts.values()),
        "entities": entities,
        "mapping_note": "红≥lock ｜ 橙≥restrict ｜ 黄≥alert ｜ 蓝<alert（阈值取自 policy.executor）",
    }
