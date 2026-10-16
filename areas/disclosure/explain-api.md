# Explain API — 证据披露协议

## 主接口

```
GET /explain?object_id=<id>&as_of=<t>
→
{
  "object_id": "...",
  "as_of": "...",
  "current": { "r_milli": 620, "veto_active": false, "policy_id": "v1" },
  "confidence": {
    "n_events": 7,
    "window_coverage": 0.8,
    "data_freshness": 0.9,
    "calibration_notes": ["交互项 fatigue×speeding 仅 3 个样本，系数未经充分校准"]
  },
  "evidence_chain": [
    { "group": "veto",    "events": [...] },
    { "group": "factors", "events": [{ "event_id", "type", "plain_text", "contribution_milli", "occurred_at", "provenance" }] },
    { "group": "interaction", "events": [...], "pair": ["fatigue","speeding"] },
    { "group": "override", "events": [...], "banner": true }
  ],
  "active_interventions": [
    { "predicate": "restrict_orders", "since": "...", "clause": "...", "release_condition": "r < 550 且工单闭环" }
  ]
}
```

## 展示规范（调度端/驾驶舱）

1. **结论行**：一句话业务语言（"限单中：疲劳驾驶 × 超速叠加，风险偏高"）。
2. **证据组**：按 veto / factors / interaction / override 分组，每条可点开看原始事件（时间、来源系统、操作人）。
3. **置信条**：n_events、窗口覆盖率、新鲜度三段式可视化；低置信必须视觉降级（不能看起来和 40 条强证据同等确定）。
4. **干预卡**：当前干预 + 条款号 + 解除条件，解除条件用业务语言（"整改工单闭环后自动解除"）。
5. **覆写横幅**：紫/橙色置顶条："此状态含人工调整（操作人、理由、到期时间）"。

## 设计红线

- 禁止只给分数不给证据；禁止把小样本系数包装成精确结论。
- 披露层的任何字段都来自内核已有数据，禁止为展示造数。
