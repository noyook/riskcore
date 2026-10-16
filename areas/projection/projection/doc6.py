"""6号文 red/yellow code list (projections.md §6号文红黄码清单).

Source: predicate clause fields + current intervention state. Generated daily
and on every change. Red = lock intervention / veto; yellow = restrict.
"""
from __future__ import annotations

from datetime import datetime

from riskfn import FUNCTION_VERSION


def build_doc6_list(
    interventions: list[dict],
    policy_id: str,
    as_of: datetime,
    regenerated_at: datetime,
) -> dict:
    """interventions: [{object_id, predicate, intervention, since, clause, release_condition}]"""
    entries = []
    for iv in sorted(interventions, key=lambda i: (i["object_id"], i["predicate"])):
        color = "red" if iv["intervention"] == "lock" else "yellow"
        entries.append(
            {
                "object_id": iv["object_id"],
                "predicate": iv["predicate"],
                "color": color,
                "color_cn": "红码" if color == "red" else "黄码",
                "clause": iv["clause"],
                "since": iv.get("since"),
                "release_condition": iv.get("release_condition", ""),
            }
        )
    red = sum(1 for e in entries if e["color"] == "red")
    return {
        "artifact": "doc6_red_yellow_list",
        "stamp": {
            "function_version": FUNCTION_VERSION,
            "policy_id": policy_id,
            "as_of": as_of.isoformat(),
            "regenerated_at": regenerated_at.isoformat(),
        },
        "as_of": as_of.isoformat(),
        "title": f"6号文红黄码清单 · 截至 {as_of.date().isoformat()}",
        "red_count": red,
        "yellow_count": len(entries) - red,
        "entries": entries,
    }
