# 事件信封（Event Envelope）v1 — 唯一写入源协议

## 信封结构

```json
{
  "event_id": "uuid，幂等键，消费方按此去重",
  "tenant_id": "租户标识，服务端强制（裸调用 401，无放行开关）",
  "trace_id": "链路追踪 ID",
  "object_id": "决策对象 ID",
  "object_type": "driver | vehicle | route | cargo | transport_task | enterprise",
  "seq": "对象内严格递增序号（服务端生成，调用方禁传）",
  "occurred_at": "业务发生时间（如罐检逾期的逾期日）",
  "ingested_at": "入日志时间，区分迟到事件",
  "type": "事件类型（见下表）",
  "payload": "类型相关的结构化数据",
  "provenance": {
    "source_system": "来源系统标识",
    "operator": "操作人/设备指纹（人工事件必填）"
  },
  "schema_version": "载荷 schema 版本"
}
```

## 事件类型字典 v1

| type | 含义 | payload 要点 | 参与加权 |
|---|---|---|---|
| telemetry | 车载遥测 | 车速/连续驾驶时长/胎压，带采集时间窗 | 是 |
| violation | 违法/报警记录 | 类型（超速/疲劳/事故/其他）、次数、时长 | 是 |
| inspection | 排查结果 | 隐患等级、位置、照片引用、hazard_ref（与工单配对） | 是 |
| registry | 监管期限事实（v1.1 增补） | kind（license/tank_inspection/insurance/road_worthiness）、expires_on、status | 是（veto 载体） |
| work_order_created | 工单创建 | 隐患/整改关联 ID | 是 |
| work_order_closed | 工单闭环 | 闭环时间、验收人 | 是（降级支付条件） |
| expert_assertion | 人工辨识断言（原 LEC-D 档案） | L/E/C 值、危险源描述、管控措施、有效期 | 是（先验） |
| correction | 纠错 | corrects_event_id、原因、更正后的载荷 | 元事件 |
| manual_override | 人工覆写 | 覆写等级、理由、审批人、有效期至 | 是（最高权重） |
| projection_only | 监管报告素材 | 任意，不参与计算 | **否** |

> v1.1 增补说明（2026-10-15）：policy v1 的否决项 tank_inspection_overdue /
> license_invalid 需要监管期限事实作为载体，原字典缺失，故增补 registry 类型。
> 变更已重过 areas/verification/ 测试集（AGENTS.md 四-5）。

## correction 链规则

- 原事件永不修改；`correction` 通过 `corrects_event_id` 指向原事件。
- 一个事件可被多次更正，以最新一条有效 correction 为准。
- 应用规则：读取时 `if correction exists → use corrected payload, keep original provenance visible`。
- correction 本身也携带 provenance（谁改的），这是审计血缘的一部分。

## 水位线与迟到事件

- 决策只消费 `seq ≤ watermark(object_id, as_of)` 的事件。
- 迟到事件（`ingested_at - occurred_at` 超过阈值）正常入日志，但不回溯已出具的决策；需要回溯时由 verification 的重放流程显式发起，并生成新的带版本钢印的修正输出。
- watermark 推进由投影器按对象推进，每次推进记录 `watermark_log`。

## 写入幂等

- 消费端按 `event_id` 去重（唯一索引）；重复投递直接确认。
- 跨系统写入采用 outbox 模式：业务事务内写日志 + outbox 记录，relay 异步投递，消费端幂等。
