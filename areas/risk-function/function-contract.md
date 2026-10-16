# 纯函数契约 risk(snapshot, policy, ctx) → RiskOutput

## 签名

```
risk(
  snapshot: StateSnapshot,     # 投影器提供的实体状态快照（含新鲜度标注）
  policy: Policy,              # 版本化政策（policy_id 已在 ctx 中）
  ctx: { object_id, as_of, policy_id, watermark }
) → RiskOutput
```

## 计算次序（严格自上而下）

1. **强制条款覆盖**：快照命中 policy.veto_types 任一 → 直接返回 r=1000，explain = 全部否决事件，veto_active=true。跳过以下所有步骤。
2. **先验注入**：对遥测缺失的 fact（新鲜度=过期/无数据），用该实体的 expert_assertion 基线并入（policy 指定注入强度系数）。
3. **单因子项**：`Σ wᵢ · gᵢ(factᵢ)`。gᵢ 为单调映射（如"距罐检逾期天数 → 越近越高"），参数在 policy.factor_params。
4. **交互项（白名单）**：`Σ λⱼₖ · interaction(factⱼ, factₖ)`，仅 policy.interaction_whitelist 内的组合。每项带 `calibration_support`（支撑样本数），披露层据此标注"未经充分校准"。
5. **饱和**：`r_milli = saturate(sum)` 压回 0–1000。
6. **置信与解释**：confidence = { n_events, window_coverage, data_freshness }；explain = 按贡献排序的事件 ID 列表（top_k，k 在 policy）。

## 输出结构

```json
{
  "r_milli": 0,
  "veto_active": false,
  "confidence": { "n_events": 0, "window_coverage": 0.0, "data_freshness": 0.0 },
  "explain": ["event_id 列表，按贡献排序"],
  "policy_id": "policy 版本",
  "as_of": "水位线时间"
}
```

## StateSnapshot 输入约定

- 每个 fact 携带三态新鲜度：`valid | stale | missing`。
- `missing` 且无可注入先验 → fact 按 policy.missing_factor 处理（默认不加分，但 confidence.coverage 如实下降）。
- 快照只读；函数对快照的任何"修改"都是 bug。

## 先验注入的语义边界

- LEC-D 基线是**传感器缺位时的信息输入**，不是装饰；注入后 r 必须有输出，即使很弱（confidence 低）。
- 先验随 expert_assertion 有效期衰减；年度辨识修订 = 追加新断言。
