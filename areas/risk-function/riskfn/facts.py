"""Fact dictionary — the shared vocabulary between projector and function.

Every weight in a policy must reference a fact defined here (loader-enforced:
no bare numbers). Each fact declares its event carrier, aggregation window
semantics and whether it is a veto carrier (强制条款).

v0 scope note: `interaction` pairs reference fact names from this table;
interaction EVALUATION is deferred to function v1.1 (W-004 scope).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FactSpec:
    name: str
    carrier: str            # event type that feeds this fact
    unit: str               # count | days_remaining | flag
    veto_carrier: str | None = None  # veto name this fact can trigger
    note: str = ""


#: Count-like facts: projector aggregates raw values + per-event ages inside the
#: scoring window; the function applies decay (policy half-life) per event.
COUNT_FACTS = (
    FactSpec("violation_speeding", "violation", "count", None, "超速违法次数（kind=speeding）"),
    FactSpec("violation_fatigue", "violation", "count", None, "疲劳驾驶违法次数（kind=fatigue）"),
    FactSpec("violation_accident", "violation", "count", "accident", "事故记录（kind=accident），命中即否决"),
    FactSpec("telemetry_fatigue_windows", "telemetry", "count", None,
             "连续驾驶超限的遥测窗口数（阈值见 policy.sensor_thresholds）"),
    FactSpec("telemetry_speeding_windows", "telemetry", "count", None,
             "车速超限的遥测窗口数（阈值见 policy.sensor_thresholds）"),
    FactSpec("open_hazard_major", "inspection", "count", "major_hazard_open",
             "未闭环重大隐患数（hazard_ref 无对应 work_order_closed）"),
    FactSpec("open_hazard_general", "inspection", "count", None, "未闭环一般隐患数"),
    FactSpec("open_work_order_overdue", "work_order", "count", None, "逾期未闭环整改工单数"),
)

#: Expiry-like facts: latest registry event per kind → days remaining at as_of.
EXPIRY_FACTS = (
    FactSpec("license_days_remaining", "registry", "days_remaining", "license_invalid",
             "证照剩余天数（registry_kind=license）；≤0 或 status≠valid 命中否决"),
    FactSpec("tank_inspection_days_remaining", "registry", "days_remaining", "tank_inspection_overdue",
             "罐检剩余天数（registry_kind=tank_inspection）；≤0 或 status≠valid 命中否决"),
)

#: Prior fact: LEC-D baseline from expert_assertion events (D-008).
PRIOR_FACTS = (
    FactSpec("lecd_prior", "expert_assertion", "prior_r_milli", None,
             "LEC-D 基线 D=L×E×C∈[1,1000]，与 r_milli 同量纲；传感器缺位时注入"),
)

ALL_FACTS = COUNT_FACTS + EXPIRY_FACTS + PRIOR_FACTS
FACT_NAMES = tuple(f.name for f in ALL_FACTS)
VETO_FACTS = {f.veto_carrier: f.name for f in ALL_FACTS if f.veto_carrier}
KNOWN_VETO_NAMES = tuple(sorted(VETO_FACTS))
