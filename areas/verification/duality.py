"""Dual-run (shadow) comparison framework (test-plan.md §4).

During the shadow period the new kernel runs alongside the legacy system and
every object's level is compared. Rules (verification area rule 2):
- differences must be explainable; explainable ones go to DECISIONS.md;
- unexplainable ones are bugs — fix the function, never patch rules.

The legacy source is a pluggable adapter: the real adapter reads the daily
risk_code_assignments snapshot (STATUS Q3, pending access). Until then the
framework ships with a synthetic adapter for format validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Mapping, Protocol

#: 新内核等级 → legacy 等级词汇（四色口径）
LEVELS = ("blue", "yellow", "orange", "red")


def level_for_r(r_milli: int, veto_active: bool, thresholds: Mapping[str, int]) -> str:
    if veto_active or r_milli >= thresholds["lock"]:
        return "red"
    if r_milli >= thresholds["restrict"]:
        return "orange"
    if r_milli >= thresholds["alert"]:
        return "yellow"
    return "blue"


@dataclass
class Diff:
    object_id: str
    new_level: str
    legacy_level: str
    new_r_milli: int | None
    classification: str  # explained_new_higher | explained_new_lower | boundary | unexplained
    note: str = ""


@dataclass
class ComparisonReport:
    day: date
    total: int
    agreed: int
    diffs: list[Diff] = field(default_factory=list)

    @property
    def agreement_rate_milli(self) -> int:
        if self.total == 0:
            return 0
        return (self.agreed * 1000) // self.total

    def render_markdown(self, policy_id: str) -> str:
        lines = [
            f"# 双跑比对日报 {self.day.isoformat()}",
            "",
            f"> policy_id={policy_id} ｜ 一致率 {self.agreement_rate_milli / 10:.1f}%"
            f"（{self.agreed}/{self.total}）",
            "",
        ]
        if not self.diffs:
            lines.append("无差异。")
        for d in self.diffs:
            lines.append(
                f"- `{d.object_id}` 新={d.new_level}(r={d.new_r_milli}) 旧={d.legacy_level} "
                f"→ **{d.classification}** {d.note}"
            )
        unexplained = [d for d in self.diffs if d.classification == "unexplained"]
        lines += [
            "",
            "## 裁决台账",
            "",
            f"- 可解释差异：{len(self.diffs) - len(unexplained)} 条（记 DECISIONS.md）",
            f"- 不可解释差异：{len(unexplained)} 条（修函数，不补规则）",
        ]
        return "\n".join(lines)


class LegacySource(Protocol):
    """Adapter over the legacy system's daily snapshot."""

    def fetch_levels(self, day: date) -> dict[str, str]: ...


class SyntheticLegacySource:
    """Format-validation adapter (NOT real data)."""

    def __init__(self, levels: dict[str, str]) -> None:
        self.levels = levels

    def fetch_levels(self, day: date) -> dict[str, str]:
        return dict(self.levels)


def compare(
    day: date,
    new_levels: Mapping[str, tuple[str, int | None]],
    legacy: LegacySource,
    *,
    thresholds: Mapping[str, int] | None = None,
    boundary_jitter_milli: int = 50,
) -> ComparisonReport:
    """new_levels: {object_id: (level, r_milli)}."""
    th = dict(thresholds or {"alert": 400, "restrict": 600, "lock": 800})
    legacy_levels = legacy.fetch_levels(day)
    report = ComparisonReport(day=day, total=len(new_levels), agreed=0)
    for oid, (level, r) in sorted(new_levels.items()):
        old = legacy_levels.get(oid)
        if old is None:
            report.diffs.append(Diff(oid, level, "absent", r, "unexplained", "legacy 无此对象快照"))
            continue
        if old == level:
            report.agreed += 1
            continue
        li, lo = LEVELS.index(level), LEVELS.index(old)
        near_boundary = r is not None and any(abs(r - t) <= boundary_jitter_milli for t in th.values())
        if near_boundary:
            cls, note = "boundary", f"r={r} 距分级边界 ≤{boundary_jitter_milli} 毫，属滞后带口径抖动"
        elif li > lo:
            cls, note = "explained_new_higher", "新内核更严（证据见 explain，通常为 veto/新因子）"
        else:
            cls, note = "explained_new_lower", "新内核更松（需在 DECISIONS 说明机理）"
        report.diffs.append(Diff(oid, level, old, r, cls, note))
    return report
