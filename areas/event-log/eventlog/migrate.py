"""Historical migration — paper forms → expert_assertion events (W-002).

Source documents: 《风险辨识记录表》 and 《车辆运输危险源清单》(items 1.1–1.10).

Design decisions:
- every paper row becomes ONE expert_assertion event (D-010: 年度辨识 =
  expert_assertion; the "two lines" concept is deleted);
- provenance marks the migration: source_system="historical-migration",
  operator = the person keying the paper form (human event, mandatory);
- event_id is DETERMINISTIC (uuid5 over tenant/object/hazard/assessment date):
  re-importing the same paper row is idempotent — duplicate=true, no double
  counting. This is what makes the reconciliation "rows == unique events"
  checkable;
- the reconciliation report is derived from the LOG itself (trace_id carries
  the batch id) — no new "current value" tables are created (W-002 deliverable 3,
  see legacy-snapshot-marking.md);
- manual fixes during migration are correction events; the report tracks the
  correction ratio.
"""
from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .envelope import EventEnvelope, Provenance
from .types import EventType, ObjectType

MIGRATION_SOURCE = "historical-migration"
BATCH_NS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")  # fixed namespace for uuid5


# --------------------------------------------------------------------------
# 《车辆运输危险源清单》1.1–1.10 模板（默认 L/E/C 为行业常见先验，用户逐车调整）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HazardItem:
    code: str
    name: str
    L: int
    E: int
    C: int
    control_hint: str = ""


VEHICLE_HAZARD_TEMPLATE: tuple[HazardItem, ...] = (
    HazardItem("1.1", "罐体/容器安全状况（腐蚀、变形、阀门泄漏）", 3, 4, 8, "定期罐检、阀门密封更换、壁厚测定"),
    HazardItem("1.2", "车辆安全装置（制动、轮胎、ABS、卫星定位）", 2, 6, 7, "一级维护、GPS 在线监控、出车前检查"),
    HazardItem("1.3", "驾驶员资质与状态（驾驶证、从业资格、疲劳）", 3, 5, 8, "资质年审、连续驾驶 4 小时强制休息、岗前问询"),
    HazardItem("1.4", "货物特性与配装（禁忌混装、超载、包装）", 2, 4, 9, "配装图审核、载重限定、包装检查"),
    HazardItem("1.5", "装卸作业（静电接地、鹤管、溢洒）", 3, 4, 7, "静置接地、操作规程、溢洒应急处置"),
    HazardItem("1.6", "行驶路线与环境（山区、临水、人员密集区、天气）", 3, 5, 7, "路线审批、恶劣天气限行、限速区段"),
    HazardItem("1.7", "超速与疲劳驾驶（动态违章）", 4, 6, 6, "遥测超限报警、违法抄告、教育培训"),
    HazardItem("1.8", "应急设备与预案（灭火器、呼吸器、堵漏、应急卡）", 2, 3, 8, "随车配备清单、月度点检、应急演练"),
    HazardItem("1.9", "停放与维修（临时停放、维修动火）", 3, 3, 6, "指定停放点、动火审批、维修台账"),
    HazardItem("1.10", "证件与监管（行驶证、营运证、罐检、押运员）", 2, 4, 8, "证照台账、临期提醒、年检计划"),
)


# --------------------------------------------------------------------------
# 草稿行（纸表 → 内存表示，未入日志）
# --------------------------------------------------------------------------


class AssertionDraft(BaseModel):
    """One paper-form row, validated but not yet an event."""

    model_config = ConfigDict(extra="forbid")

    object_id: str = Field(min_length=1)
    object_type: ObjectType = ObjectType.VEHICLE
    hazard_source: str = Field(min_length=1)
    L: int = Field(ge=1, le=10)
    E: int = Field(ge=1, le=10)
    C: int = Field(ge=1, le=10)
    control_measures: str = Field(min_length=1)
    valid_until: date
    assessed_at: date
    operator: str = Field(min_length=1)
    item_code: str | None = None  # 清单条目号（1.1–1.10），可追溯纸面位置

    def deterministic_event_id(self, tenant_id: str) -> uuid.UUID:
        key = f"{tenant_id}:{self.object_id}:{self.hazard_source}:{self.assessed_at.isoformat()}"
        return uuid.uuid5(BATCH_NS, key)


class ParseError(BaseModel):
    line: int
    text: str
    error: str


class ParseResult(BaseModel):
    drafts: list[AssertionDraft] = Field(default_factory=list)
    errors: list[ParseError] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_tsv(text: str, *, operator: str, assessed_at: date, valid_until: date,
              object_type: str = "vehicle", default_measures: str = "见纸质记录表") -> ParseResult:
    """Parse pasted paper rows: object_id, item_code?, hazard_source, L, E, C, measures?, valid_until?

    Tab or comma separated. Lines starting with # are comments. item_code may be
    a bare number (1.1) or empty. When measures/valid_until are omitted the
    defaults apply (paper forms often carry them once in a header).
    """
    result = ParseResult()
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    for lineno, row in enumerate(reader, start=1):
        cells = [c.strip() for c in row if c.strip() != ""]
        if not cells or cells[0].startswith("#"):
            continue
        try:
            # 兼容逗号分隔（csv 按 tab 读时整行会落在一个 cell 里）
            if len(cells) == 1 and "," in cells[0]:
                cells = [c.strip() for c in cells[0].split(",") if c.strip()]
            if len(cells) < 5:
                raise ValueError("至少需要 5 列：object_id, [条目号,] 危险源, L, E, C")
            object_id = cells[0]
            item_code = None
            idx = 1
            if len(cells) >= 6 and cells[1].replace(".", "", 1).isdigit():
                item_code = cells[1]
                idx = 2
            hazard_source = cells[idx]
            L, E, C = int(cells[idx + 1]), int(cells[idx + 2]), int(cells[idx + 3])
            measures = cells[idx + 4] if len(cells) > idx + 4 else default_measures
            vu = date.fromisoformat(cells[idx + 5]) if len(cells) > idx + 5 else valid_until
            result.drafts.append(
                AssertionDraft(
                    object_id=object_id,
                    object_type=ObjectType(object_type),
                    hazard_source=hazard_source,
                    L=L, E=E, C=C,
                    control_measures=measures,
                    valid_until=vu,
                    assessed_at=assessed_at,
                    operator=operator,
                    item_code=item_code,
                )
            )
        except Exception as exc:
            result.errors.append(ParseError(line=lineno, text=",".join(cells)[:120], error=str(exc)))
    return result


# --------------------------------------------------------------------------
# 灌入 + 对账
# --------------------------------------------------------------------------


class AppendLike(Protocol):
    def append(self, event: EventEnvelope): ...


@dataclass
class IngestRow:
    object_id: str
    item_code: str | None
    hazard_source: str
    event_id: str
    seq: int | None
    duplicate: bool
    status: str  # created | duplicate | failed
    error: str | None = None


@dataclass
class MigrationReport:
    batch_id: str
    operator: str
    submitted_rows: int = 0
    created_events: int = 0
    duplicate_rows: int = 0
    failed_rows: int = 0
    rows: list[IngestRow] = field(default_factory=list)
    corrections: int = 0  # 迁移期内人工修正事件数（按 batch 统计）

    @property
    def correction_ratio_milli(self) -> int:
        if self.submitted_rows == 0:
            return 0
        return (self.corrections * 1000) // self.submitted_rows

    def summary(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "operator": self.operator,
            "submitted_rows": self.submitted_rows,
            "created_events": self.created_events,
            "duplicate_rows": self.duplicate_rows,
            "failed_rows": self.failed_rows,
            "reconciled": self.created_events + self.duplicate_rows == self.submitted_rows,
            "corrections": self.corrections,
            "correction_ratio_milli": self.correction_ratio_milli,
        }


def ingest_drafts(
    store: AppendLike,
    drafts: Iterable[AssertionDraft],
    *,
    tenant_id: str,
    operator: str,
    batch_id: str | None = None,
) -> MigrationReport:
    batch_id = batch_id or f"mig-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    report = MigrationReport(batch_id=batch_id, operator=operator)
    for draft in drafts:
        report.submitted_rows += 1
        event_id = draft.deterministic_event_id(tenant_id)
        envelope = EventEnvelope(
            event_id=event_id,
            tenant_id=tenant_id,
            trace_id=f"migrate-{batch_id}",
            object_id=draft.object_id,
            object_type=draft.object_type,
            occurred_at=datetime.combine(draft.assessed_at, datetime.min.time(), tzinfo=timezone.utc),
            ingested_at=datetime.now(timezone.utc),
            type=EventType.EXPERT_ASSERTION,
            payload={
                "hazard_source": draft.hazard_source,
                "control_measures": draft.control_measures,
                "L": draft.L,
                "E": draft.E,
                "C": draft.C,
                "valid_until": draft.valid_until.isoformat(),
            },
            provenance=Provenance(source_system=MIGRATION_SOURCE, operator=draft.operator),
        )
        try:
            result = store.append(envelope)
            row = IngestRow(
                object_id=draft.object_id,
                item_code=draft.item_code,
                hazard_source=draft.hazard_source,
                event_id=str(event_id),
                seq=result.seq,
                duplicate=result.duplicate,
                status="duplicate" if result.duplicate else "created",
            )
            if result.duplicate:
                report.duplicate_rows += 1
            else:
                report.created_events += 1
        except Exception as exc:  # 单行失败不阻断整批
            report.failed_rows += 1
            row = IngestRow(
                object_id=draft.object_id,
                item_code=draft.item_code,
                hazard_source=draft.hazard_source,
                event_id=str(event_id),
                seq=None,
                duplicate=False,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        report.rows.append(row)
    return report


def batch_correction_count(store, tenant_id: str, batch_id: str) -> int:
    """Count correction events created under this migration batch's trace."""
    n = 0
    for oid in store.iter_objects(tenant_id):
        for ev in store.read_object_events(tenant_id, oid):
            if ev.type is EventType.CORRECTION and ev.trace_id == f"migrate-{batch_id}":
                n += 1
    return n
