"""LEC-D annual risk assessment report (projections.md §年度风险辨识评估报告).

Source: the full set of expert_assertion events, re-arranged into LEC-D
chapters. Annual trigger; manually regenerable. Old versions are archived,
never overwritten (projections area rule: 修正旧产物 = 重生成新版本).
"""
from __future__ import annotations

from datetime import datetime

from eventlog import EventEnvelope, EventType, parse_payload

from riskfn import FUNCTION_VERSION


def _d_band(d: int) -> str:
    if d >= 320:
        return "显著危险（需要立即整改）"
    if d >= 160:
        return "高度危险（需要采取措施）"
    if d >= 70:
        return "一般危险（需要注意）"
    return "稍有危险（可以接受）"


def build_lecd_report(
    events: list[EventEnvelope], policy_id: str, as_of: datetime, regenerated_at: datetime
) -> dict:
    """events: expert_assertion events for the tenant (all objects)."""
    chapters = []
    for ev in sorted(events, key=lambda e: (e.object_id, e.seq)):
        if ev.type is not EventType.EXPERT_ASSERTION:
            continue
        p = parse_payload(EventType.EXPERT_ASSERTION, ev.payload)
        d = p.d_score
        chapters.append(
            {
                "object_id": ev.object_id,
                "event_id": str(ev.event_id),
                "hazard_source": p.hazard_source,
                "L": p.L,
                "E": p.E,
                "C": p.C,
                "D": d,
                "risk_band": _d_band(d),
                "control_measures": p.control_measures,
                "valid_until": str(p.valid_until),
                "assessed_by": ev.provenance.operator,
                "assessed_at": ev.occurred_at.isoformat(),
            }
        )
    return {
        "artifact": "lecd_annual_report",
        "stamp": {
            "function_version": FUNCTION_VERSION,
            "policy_id": policy_id,
            "as_of": as_of.isoformat(),
            "regenerated_at": regenerated_at.isoformat(),
        },
        "as_of": as_of.isoformat(),
        "title": f"年度风险辨识评估报告（LEC-D）· 截至 {as_of.date().isoformat()}",
        "chapter_count": len(chapters),
        "chapters": chapters,
    }


def render_markdown(report: dict) -> str:
    """Human-readable rendering (纸形真相)."""
    lines = [
        f"# {report['title']}",
        "",
        f"> 版本钢印：function_version={report['stamp']['function_version']} | "
        f"policy_id={report['stamp']['policy_id']} | as_of={report['stamp']['as_of']} | "
        f"regenerated_at={report['stamp']['regenerated_at']}",
        "",
        f"共辨识危险源 {report['chapter_count']} 项。",
        "",
    ]
    for i, ch in enumerate(report["chapters"], 1):
        lines += [
            f"## {i}. {ch['hazard_source']}（对象 {ch['object_id']}）",
            "",
            f"- 风险值 D = L({ch['L']}) × E({ch['E']}) × C({ch['C']}) = **{ch['D']}** —— {ch['risk_band']}",
            f"- 管控措施：{ch['control_measures']}",
            f"- 辨识人：{ch['assessed_by']} ｜ 辨识时间：{ch['assessed_at'][:10]} ｜ 有效期至：{ch['valid_until']}",
            f"- 审计下钻：事件 `{ch['event_id']}`",
            "",
        ]
    return "\n".join(lines)
