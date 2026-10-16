"""Predicate engine — 滞后带 / fail-closed / 覆写 / 条款+证据强制 (W-006).

Executor area rules made executable:
  R1 输入只有 RiskOutput（投影器缓存）+ 谓词声明 + 投影器维护的 override 状态；
     引擎绝不调用纯函数、绝不读日志。
  R2 滞后只加在"是否触发/解除动作"上，不回写 r。
  R3 veto_active=true → 最高干预，无滞后豁免。
  R4 取不到 r（缓存缺失 / state_as_of 超 stale_after）→ 按 fallback（默认 deny）。
  R5 每个动作必须携带 clause 与 explain_refs，缺一项即拒绝执行
     （MissingEvidenceError；fallback/override 动作除外，其 reason_code 自证）。
  R6 动作后果由调用方回流日志——引擎只写自己的 intervention_state。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .expressions import ExpressionError, eval_bool

PREDICATES_DIR = Path(__file__).resolve().parent.parent / "predicates"

Intervention = Literal["none", "alert", "restrict", "lock"]


class Predicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    object_type: str
    deny_when: str
    warn_when: str | None = None
    #: which policy threshold band this predicate's hysteresis uses
    bound: Literal["alert", "restrict", "lock"]
    intervention: Literal["alert", "restrict", "lock"]
    fallback: Literal["deny", "allow"] = "deny"
    stale_after_seconds: int = Field(ge=1)
    clause: str = Field(min_length=1)
    actions: list[str] = Field(default_factory=list)
    #: enforce = 真拦截（W-010 起）；observe = 只记不拦（W-006 旁路观察）
    mode: Literal["enforce", "observe"] = "enforce"


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert: int
    restrict: int
    lock: int
    hysteresis_milli: int = 0


@dataclass
class HysteresisState:
    intervention: str  # none | alert | restrict | lock
    r_at_entry: int | None
    since: datetime | None


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicate: str
    object_id: str
    allow: bool
    enforced: bool
    intervention: str
    reason_code: str
    clause: str
    explain_refs: list[str] = Field(default_factory=list)
    state_as_of: datetime | None = None
    r_milli: int | None = None
    veto_active: bool = False
    override: dict | None = None
    warning: bool = False


class MissingEvidenceError(RuntimeError):
    """R5: an enforcing action without clause+explain_refs is refused."""


class DecisionLike(Protocol):
    r_milli: int
    veto_active: bool
    as_of: datetime
    explain: list


class OverrideLike(Protocol):
    level: str
    operator: str
    approver: str
    reason: str
    expires_at: datetime
    event_id: str


def load_predicates(predicates_dir: Path | None = None) -> dict[str, Predicate]:
    base = predicates_dir or PREDICATES_DIR
    out: dict[str, Predicate] = {}
    for path in sorted(base.glob("*.json")):
        pred = Predicate.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if pred.name != path.stem:
            raise ValueError(f"predicate file {path.name} declares name '{pred.name}'")
        out[pred.name] = pred
    return out


class PredicateEngine:
    """Stateless evaluation; hysteresis state is read/written via a store port."""

    def __init__(self, thresholds: Thresholds) -> None:
        self.thresholds = thresholds

    # ------------------------------------------------------------------ core

    def evaluate(
        self,
        predicate: Predicate,
        decision: DecisionLike | None,
        override: OverrideLike | None,
        state: HysteresisState | None,
        now: datetime,
        object_id: str = "",
    ) -> Verdict:
        th = self.thresholds
        namespace = {
            "veto_active": bool(decision.veto_active) if decision else False,
            "r_milli": decision.r_milli if decision else 0,
            "alert_threshold": th.alert,
            "restrict_threshold": th.restrict,
            "lock_threshold": th.lock,
            "hysteresis_milli": th.hysteresis_milli,
        }

        # ---- R4: fail-closed — 取不到 r 或状态过期 -------------------------
        stale = (
            decision is None
            or (now - decision.as_of).total_seconds() > predicate.stale_after_seconds
        )
        if stale:
            return self._verdict(
                predicate,
                object_id=object_id,
                intervention="lock" if predicate.fallback == "deny" else "none",
                reason_code="stale_fallback",
                decision=decision,
                override=override,
                state_as_of=decision.as_of if decision else None,
                r_milli=decision.r_milli if decision else None,
                veto_active=bool(decision.veto_active) if decision else False,
            )

        # ---- 人工覆写：最高优先级，r 不参与 ----------------------------------
        if override is not None:
            level = override.level
            intervention = "none" if level == "clear" else level
            return self._verdict(
                predicate,
                object_id=object_id,
                intervention=intervention,
                reason_code=f"override_{level}",
                decision=decision,
                override=override,
                explain_refs=[override.event_id],
                state_as_of=decision.as_of,
                r_milli=decision.r_milli,
                veto_active=decision.veto_active,
            )

        # ---- 谓词求值 -------------------------------------------------------
        try:
            deny_fire = eval_bool(predicate.deny_when, namespace)
            warn_fire = (
                eval_bool(predicate.warn_when, namespace) if predicate.warn_when else False
            )
        except ExpressionError as exc:
            raise ExpressionError(f"predicate '{predicate.name}': {exc}") from exc

        current = state.intervention if state else "none"
        enter_bound = getattr(th, predicate.bound)
        exit_bound = enter_bound - th.hysteresis_milli

        if decision.veto_active:
            # R3: 强制条款命中 → 最高干预，无滞后豁免
            intervention, reason = predicate.intervention, "veto_active"
        elif deny_fire:
            if current == "none":
                intervention, reason = predicate.intervention, "entered"
            else:
                intervention, reason = current, "hysteresis_hold"
        elif current != "none":
            if decision.r_milli < exit_bound:
                intervention, reason = "none", "released"
            else:
                intervention, reason = current, "hysteresis_hold"
        else:
            intervention = "none"
            reason = "advisory_warning" if warn_fire else "ok"

        return self._verdict(
            predicate,
            object_id=object_id,
            intervention=intervention,
            reason_code=reason,
            decision=decision,
            override=None,
            warning=bool(warn_fire) and intervention == "none",
            state_as_of=decision.as_of,
            r_milli=decision.r_milli,
            veto_active=decision.veto_active,
        )

    # ---------------------------------------------------------------- verdict

    def _verdict(
        self,
        predicate: Predicate,
        *,
        object_id: str,
        intervention: str,
        reason_code: str,
        decision: DecisionLike | None,
        override: OverrideLike | None,
        explain_refs: list[str] | None = None,
        state_as_of: datetime | None = None,
        r_milli: int | None = None,
        veto_active: bool = False,
        warning: bool = False,
    ) -> Verdict:
        refs = list(explain_refs) if explain_refs is not None else (
            [e.event_id if hasattr(e, "event_id") else str(e) for e in (decision.explain or [])]
            if decision is not None
            else []
        )
        enforced = predicate.mode == "enforce"

        # R5: 执行态动作必须带 clause + explain_refs（fallback/override 自证除外）
        if predicate.mode == "enforce" and intervention != "none" and reason_code not in ("stale_fallback",) and override is None:
            if not predicate.clause or not refs:
                raise MissingEvidenceError(
                    f"predicate '{predicate.name}' refusing to act: "
                    f"clause/explain_refs incomplete (reason={reason_code})"
                )

        # 旁路观察模式（W-006）：会拦截也不拦，只留痕；allow 恒真
        if predicate.mode == "observe":
            enforced = False
            allow = True
        else:
            enforced = True
            allow = intervention != "lock"

        return Verdict(
            predicate=predicate.name,
            object_id=object_id or (getattr(decision, "object_id", "") if decision is not None else ""),
            allow=allow,
            enforced=enforced,
            intervention=intervention,
            reason_code=reason_code,
            clause=predicate.clause,
            explain_refs=refs,
            state_as_of=state_as_of,
            r_milli=r_milli,
            veto_active=veto_active,
            override=(
                {
                    "level": override.level,
                    "operator": override.operator,
                    "approver": override.approver,
                    "reason": override.reason,
                    "expires_at": override.expires_at.isoformat(),
                    "event_id": override.event_id,
                }
                if override is not None
                else None
            ),
            warning=warning,
        )
