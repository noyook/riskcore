# 状态库投影器（Projector）与对账守护

## 职责

日志是本体；投影器负责把日志加工成两类**可丢弃的**读模型：

### 1. fact 快照表（给纯函数的输入）

每实体 × 每 fact 一行：

```
object_id | fact_name | current_value | last_updated | ttl | source_event_id
```

- 由事件流增量维护：事件到达 → 更新对应 fact 行。
- fact 字典与新鲜度语义见 areas/risk-function/function-contract.md 的输入约定。

### 2. 决策状态缓存（给执行器的输入）

```
object_id | r_milli | confidence | explain_refs | as_of | seq_upto | policy_id | computed_at
```

- 由纯函数输出物化，带 TTL；执行器只读此缓存（见 executor 区域）。
- 缓存条目到期后可按需重算；重算 = 对同一 `as_of + policy_id` 再跑一遍函数。

## 对账守护（Reconciler）—— 铁律三的工程实现

1. 每 N 分钟（默认 10）抽样对象做**全量重算**：从日志重放窗口聚合 → 与快照表/缓存比对。
2. 每日低峰做一次全量对象对账。
3. 差异 ≠ 0 → 告警，并以重算结果修正缓存；差异模式入库，连续漂移触发事故复盘。
4. 对账结果本身写成 `projection_only` 事件留痕。

## 禁止事项

- 禁止其他区域直接 UPDATE 快照表（只有投影器写）。
- 禁止把快照表当主数据对外承诺（对外承诺永远是"可重放"）。
