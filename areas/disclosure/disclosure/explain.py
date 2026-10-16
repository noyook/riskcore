"""ExplainService — builds the disclosure payload from kernel data (W-007).

Disclosure area rules:
  R1 只读：本服务只经 投影器/执行器/日志 的读接口取数，无任何写路径。
  R2 一切面向人的数字带上下文（r 旁边必有 confidence 与证据条数）。
  R3 人工覆写永远置顶（override banner）。
  R2' 业务语言：plain_text 是人类句子，事件 ID / 条款号只作下钻入口。
  红线：禁止只给分数不给证据；禁止为展示造数（每个字段都来自内核已有数据）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from eventlog import EventEnvelope, EventType, SqliteStore, apply_corrections, parse_payload

KIND_CN = {
    "speeding": "超速",
    "fatigue": "疲劳驾驶",
    "overload": "超载",
    "illegal_parking": "违规停车",
    "accident": "事故",
    "other": "其他违法",
}
LEVEL_CN = {"major": "重大", "general": "一般", "minor": "轻微"}
REGISTRY_CN = {
    "license": "证照",
    "tank_inspection": "罐检",
    "insurance": "保险",
    "road_worthiness": "年检",
}
OVERRIDE_CN = {"alert": "预警", "restrict": "限单", "lock": "锁定", "clear": "解除"}


class ConfidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_events: int
    window_coverage_milli: int
    data_freshness_milli: int
    calibration_notes: list[str] = Field(default_factory=list)


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    type: str
    plain_text: str
    contribution_milli: int | None = None
    occurred_at: datetime
    provenance: dict


class EvidenceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: str
    label: str
    events: list[EvidenceEvent] = Field(default_factory=list)
    banner: bool = False
    pair: list[str] | None = None


class InterventionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    predicate: str
    intervention: str
    since: str | None = None
    clause: str
    release_condition: str


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_id: str
    as_of: datetime | None = None
    current: dict
    conclusion: str
    confidence: ConfidenceView
    evidence_chain: list[EvidenceGroup] = Field(default_factory=list)
    active_interventions: list[InterventionView] = Field(default_factory=list)
    low_confidence: bool = False


class DecisionReader(Protocol):
    def read_decision(self, object_id: str): ...


class SnapshotReader(Protocol):
    def read_snapshot(self, object_id: str): ...


class OverrideReader(Protocol):
    def read_override(self, object_id: str): ...


class InterventionReader(Protocol):
    def active_interventions(self, object_id: str) -> list[dict]: ...


def _plain_text(ev: EventEnvelope) -> str:
    p = ev.payload
    t = ev.type
    if t is EventType.TELEMETRY:
        parts = []
        if p["continuous_driving_minutes"] > 0:
            parts.append(f"连续驾驶 {p['continuous_driving_minutes']} 分钟")
        if p["speed_max_kmh"] > 0:
            parts.append(f"最高车速 {p['speed_max_kmh']} km/h")
        return "遥测：" + "，".join(parts) if parts else "遥测数据"
    if t is EventType.VIOLATION:
        return (
            f"{KIND_CN.get(p['violation_kind'], p['violation_kind'])}违法 "
            f"{p['count']} 次（累计 {p['duration_minutes']} 分钟）"
        )
    if t is EventType.INSPECTION:
        return f"{LEVEL_CN.get(p['hazard_level'], p['hazard_level'])}隐患：{p['location']}"
    if t is EventType.WORK_ORDER_CREATED:
        return f"整改工单创建（关联 {p['hazard_ref']}，限期 {p['due_date']}）"
    if t is EventType.WORK_ORDER_CLOSED:
        return f"整改工单闭环（{p['closes_order_id']}，验收人 {p['acceptor']}）"
    if t is EventType.EXPERT_ASSERTION:
        return f"年度辨识：{p['hazard_source']}（LEC-D 风险值 D={p['L'] * p['E'] * p['C']}）"
    if t is EventType.REGISTRY:
        status_cn = {"valid": "有效", "expired": "已过期", "revoked": "已注销"}.get(p["status"], p["status"])
        return f"{REGISTRY_CN.get(p['registry_kind'], p['registry_kind'])}有效期至 {p['expires_on']}（{status_cn}）"
    if t is EventType.CORRECTION:
        return f"纠错：{p['reason']}"
    if t is EventType.MANUAL_OVERRIDE:
        return (
            f"人工覆写（{OVERRIDE_CN.get(p['override_level'], p['override_level'])}）："
            f"{p['reason']}，审批人 {p['approver']}，到期 {p['expires_at'][:10]}"
        )
    if t is EventType.PROJECTION_ONLY:
        return f"监管素材：{p.get('material_kind', 'unspecified')}"
    return t.value


class ExplainService:
    def __init__(self, store: SqliteStore, projector, executor, policy, tenant_id: str) -> None:
        self.store = store
        self.projector = projector
        self.executor = executor
        self.policy = policy
        self.tenant = tenant_id

    # ------------------------------------------------------------------ build

    def explain(self, object_id: str, as_of: datetime | None = None) -> ExplainResponse:
        decision = self.projector.read_decision(object_id)
        snapshot = self.projector.read_snapshot(object_id)
        override = self.projector.read_override(object_id)
        interventions = self.executor.active_interventions(object_id)
        raw_events = self.store.read_object_events(self.tenant, object_id)
        # 证据链必须展示"生效载荷"（correction 链解析后）——否则证据与分数自相矛盾
        events = {str(e.event_id): e for e in apply_corrections(list(raw_events))}

        current = {
            "r_milli": decision.r_milli if decision else None,
            "veto_active": bool(decision.veto_active) if decision else None,
            "policy_id": self.policy.policy_id,
            "state_as_of": decision.as_of.isoformat() if decision else None,
        }

        # 证据链分组（veto / factors(含 prior) / override；interaction v1.1 开）
        groups: dict[str, EvidenceGroup] = {}
        if decision is not None:
            for ref in decision.explain:
                ref_d = ref if isinstance(ref, dict) else ref.model_dump()
                ev = events.get(ref_d["event_id"])
                if ev is None:
                    continue
                entry = EvidenceEvent(
                    event_id=ref_d["event_id"],
                    type=ev.type.value,
                    plain_text=_plain_text(ev),
                    contribution_milli=ref_d.get("contribution_milli"),
                    occurred_at=ev.occurred_at,
                    provenance=ev.provenance.model_dump(mode="json"),
                )
                kind = ref_d.get("kind", "factor")
                if kind == "veto":
                    g = groups.setdefault("veto", EvidenceGroup(group="veto", label="否决证据（强制条款）"))
                elif kind == "prior":
                    g = groups.setdefault("prior", EvidenceGroup(group="prior", label="先验注入（LEC-D 基线）"))
                else:
                    g = groups.setdefault("factors", EvidenceGroup(group="factors", label="风险因子"))
                g.events.append(entry)

        if override is not None:
            ev = events.get(override.event_id)
            if ev is not None:
                groups["override"] = EvidenceGroup(
                    group="override",
                    label="人工覆写（置顶）",
                    banner=True,
                    events=[
                        EvidenceEvent(
                            event_id=override.event_id,
                            type=ev.type.value,
                            plain_text=_plain_text(ev),
                            occurred_at=ev.occurred_at,
                            provenance=ev.provenance.model_dump(mode="json"),
                        )
                    ],
                )

        # 覆写横幅永远置顶（区域规则 3）
        ordered = []
        if "override" in groups:
            ordered.append(groups.pop("override"))
        ordered.extend(groups[k] for k in ("veto", "prior", "factors", "interaction") if k in groups)

        # 置信度 + 校准标注
        conf = decision.confidence if decision else None
        notes: list[str] = []
        for rule in self.policy.scoring.interaction_whitelist:
            if rule.calibration_support < 10:
                notes.append(
                    f"交互项 {'×'.join(rule.pair)} 仅 {rule.calibration_support} 个样本支撑，"
                    "系数未经充分校准（函数 v1.1 起求值）"
                )
        confidence = ConfidenceView(
            n_events=conf.n_events if conf else 0,
            window_coverage_milli=conf.window_coverage_milli if conf else 0,
            data_freshness_milli=conf.data_freshness_milli if conf else 0,
            calibration_notes=notes,
        )
        low_confidence = confidence.data_freshness_milli < 500 or confidence.window_coverage_milli < 500

        return ExplainResponse(
            object_id=object_id,
            as_of=(as_of or (decision.as_of if decision else None)),
            current=current,
            conclusion=self._conclusion(decision, interventions, override),
            confidence=confidence,
            evidence_chain=ordered,
            active_interventions=[
                InterventionView(
                    predicate=i["predicate"],
                    intervention=i["intervention"],
                    since=i.get("since"),
                    clause=i["clause"],
                    release_condition=i["release_condition"],
                )
                for i in interventions
            ],
            low_confidence=low_confidence,
        )

    # ------------------------------------------------------------- conclusion

    def _conclusion(self, decision, interventions, override) -> str:
        if decision is None:
            return "无决策记录：该对象尚未投影（fail-closed 期间按最严处理）"
        r = decision.r_milli
        names_cn = {
            "accident": "事故记录",
            "major_hazard_open": "重大隐患未闭环",
            "tank_inspection_overdue": "罐检逾期",
            "license_invalid": "证照失效",
        }
        underlying = None
        if decision.veto_active:
            reason = "、".join(names_cn.get(v, v) for v in (decision.veto_names or []))
            underlying = f"强制条款命中：{reason}（r=1000/1000）"

        # 覆写置顶（区域规则 3），但底层状态必须如实并列——不遮遮掩掩（D-009）
        if override is not None:
            text = (
                f"人工调整中：{OVERRIDE_CN.get(override.level, override.level)}"
                f"（操作人 {override.operator}，到期 {override.expires_at.strftime('%m-%d %H:%M')}）"
            )
            if underlying:
                text += f"；底层状态：{underlying}"
            return text

        if decision.veto_active:
            return f"已锁定：{underlying}"
        if interventions:
            top = interventions[0]
            level_cn = {"alert": "预警", "restrict": "限单", "lock": "锁定"}.get(top["intervention"], top["intervention"])
            return f"{level_cn}中：风险偏高（r={r}/1000，条款 {top['clause']}）"
        if r >= self.policy.executor.thresholds["restrict"]:
            return f"观察中：风险偏高（r={r}/1000）"
        return f"正常放行（r={r}/1000，详见证据链）"

    # -------------------------------------------------------------------- api

    def build_router(self):
        from fastapi import APIRouter, Query

        service = self
        router = APIRouter(tags=["disclosure"])

        @router.get("/explain", response_model=None)
        def explain(object_id: str = Query(...), as_of: str | None = Query(None)) -> dict:
            parsed = datetime.fromisoformat(as_of) if as_of else None
            resp = service.explain(object_id, parsed)
            return resp.model_dump(mode="json")

        return router
