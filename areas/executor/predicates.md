# 谓词声明库（Predicates as Data）

## 声明格式

```json
{
  "name": "allow_dispatch",
  "object_type": "transport_task",
  "deny_when":  "veto_active OR r_milli >= 800",
  "warn_when":  "r_milli >= 600",
  "fallback":   "deny",
  "stale_after": "15m",
  "clause":     "6号文-第Y条",
  "actions":    ["reject_with_reason", "notify_dispatcher"]
}
```

## 内置谓词 v1

| name | deny_when | 动作 | clause 示例 |
|---|---|---|---|
| allow_dispatch | veto 或 r ≥ lock 线 | 拒派 + 通知调度 + 出示证据链 | 6号文-第Y条 |
| restrict_orders | r ≥ restrict 线 | 限单/调单 + 预警相关岗位 | 内部风控线 |
| lock_vehicle | veto 或 r ≥ lock 线 | 锁车 + 建整改工单 | 6号文-第Y条 |
| alert_single_fact | 单项 fact 越限（罐检逾期、证照失效） | 直发预警，不等综合 r | 6号文-第X条 |

## 滞后带语义（取代"逐级缓降状态机"）

- **触发**：r 越过上界才干预。
- **解除**：已干预实体需 r 跌破**下界**（上界 − hysteresis_milli）才解除。
- 上界/下界之间为回滞带，带内维持现状——抖动在决策层被物理吸收。
- 解除的额外支付条件（policy 可配）：相关工单存在 work_order_closed 事件。闭环不完成，干预压不下去。

## fail-closed 与新鲜度

- 谓词返回体必须带 `state_as_of`；`now - state_as_of > stale_after` → 视为取不到 r，走 fallback。
- 未知实体（无快照、无先验）→ fallback = deny + 提示先完成辨识。

## 人工覆写的执行语义

- manual_override 事件由投影器转为最高优先级输入：override 未到期 → 按 override 等级求值，r 不参与。
- 每次基于 override 的动作，返回体中 `override = {operator, reason, expires_at}` 并置顶展示。
- override 到期自动失效，恢复函数求值；不自动续期。
