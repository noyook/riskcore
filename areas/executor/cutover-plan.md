# W-010 切流方案 — allow_dispatch 旁路 → fail-closed 拦截

> 前置门禁（本文件即检查单）：两项未过不得切流。
> 1. **W-007 披露层人工试用**：真实调度员能在 30 秒内出示完整证据链（STATUS.md 追踪）；
> 2. **W-009 双跑 14 天**：差异全部有裁决记录、不可解释差异为 0。

## 1. 切流机制（已就绪）

谓词模式是**数据开关**：`areas/executor/predicates/allow_dispatch.json` 的 `mode`
字段，运行时由 `RISKCORE_PREDICATE_MODE` 环境变量统一覆盖（`observe` | `enforce`）。
不允许单谓词绕过——切流是受控的整体动作。

```bash
# 切流（enforce）：重启即生效
RISKCORE_PREDICATE_MODE=enforce python3 scripts/serve.py --db .tmp/riskcore.db

# 回滚（observe）：一键
scripts/rollback_observe.sh
```

## 2. 灰度阶段

| 阶段 | 范围 | 时长 | 通过标准 |
|---|---|---|---|
| S0 影子 | 全量 observe | ≥14 天（W-009） | 一致率达标、不可解释差异=0、连续裁决记录 |
| S1 内部车队 | 本公司自营车（object_type=vehicle 且 tenant 内网段） | 3 天 | 红码拦截 0 逃逸、调度员无证据链投诉、回滚未触发 |
| S2 全量 | 全部承运商 | 持续 | 同上 + 周报审阅 |

## 3. 切流时行为变化（对调度员）

- enforce 后：红码（veto 或 r≥800）派单请求返回 `allow=false`，
  `intervention=lock`，`clause` + `explain_refs` 随拒派理由返回（R5：
  缺 clause/证据即拒绝执行——系统不会"无声拦截"）；
- 滞后带保持：进入 800、解除 750，带内不抖动；
- fail-closed：缓存缺失/过期 → deny + stale_fallback；
- 人工覆写仍可解除（override 置顶披露，72h 到期自动失效）。

## 4. 回滚预案

- **一键回滚**：`scripts/rollback_observe.sh`（改环境变量并重启；obs reveal 期
  语义 = 恢复"只记不拦"）。
- **回滚时效**：配置级开关，重启即恢复，目标 ≤15 分钟（实测见演练记录）。
- **回滚后**：旁路观察继续留痕，差异重新进入双跑语料，不影响已出具决策
  （决策历史独立审计，D-002）。

## 5. 演练记录（2026-10-16，测试环境）

见 outputs/2026-10-16-切流演练记录.md：
enforce 下红码对象被物理拦截且证据链可见；回滚后恢复放行；
全程计时。

## 6. 上线检查单

- [ ] S0 双跑 14 天完成且裁决台账齐全
- [ ] 披露层人工试用通过（调度员签字/记录）
- [ ] S1 内部车队 3 天无逃逸、无投诉
- [ ] 回滚演练 ≤15 分钟通过
- [ ] lock_vehicle 谓词同步 enforce（锁车与拒派同权）
- [ ] 值班接入漂移告警（对账守护）
