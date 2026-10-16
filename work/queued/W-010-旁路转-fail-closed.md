# W-010 旁路转 fail-closed 拦截

- 状态：queued ｜ 里程碑：M4 ｜ 依赖：W-007、W-009

## 目标
allow_dispatch 从"只记不拦"切换为真实拒派；锁车谓词上线。

## 产出
1. 切流方案（灰度：先内部车队 → 全量）。
2. 锁车谓词 lock_vehicle 接入（强制条款命中即锁）。
3. 回滚预案（一键退回旁路模式）。

## 验收
- 红码车辆派单被物理拦截，调度端即时可见证据链。
- 回滚演练通过（15 分钟内恢复旁路）。

## 红线
披露层（W-007）未稳定运行前，本任务不得启动。

---

## 阻塞记录（2026-10-15）

- 红线遵守中：W-007 披露层代码与接口验收已完成，但**真实调度员试用验收未执行**
（无人可做），按任务红线"披露层未稳定运行前不得启动"，本任务保持 queued。
- W-009 双跑（14 天）未完成，同为前置阻塞。
- 切流所需基础设施已就绪：谓词 mode=observe→enforce 是数据开关
  （areas/executor/predicates/allow_dispatch.json），回滚 = 改回 observe，
  15 分钟回滚演练可在 enforce 切换后随时执行。

---

## 进展记录 2（2026-10-16 晚）：切流基础设施 + 演练完成

**新增交付**：
- 环境变量配置层：`RISKCORE_DB/TENANT/PORT/TICK_SECONDS/PREDICATE_MODE`（serve.py
  启动时集中校验、快速失败；`.env.example` 占位模板）。
- 切流开关：`RISKCORE_PREDICATE_MODE=observe|enforce` 统一覆盖全部谓词
  （不允许单谓词绕过——受控动作）。
- `areas/executor/cutover-plan.md`：灰度 S0 影子 → S1 内部车队 → S2 全量，
  每阶段通过标准 + 上线检查单。
- `scripts/rollback_observe.sh`：一键回滚（.env + 停进程 + JSON 兜底）。
- 演练（`outputs/2026-10-16-切流演练记录.md`）：enforce 下红码 allow=false
  （条款+证据随行）、干净对象不误伤、锁车谓词同权；回滚 **70 秒**
  （目标 ≤15 分钟），回滚后旁路留痕继续。

**仍未完成（外部依赖）**：S0 双跑 14 天（W-009）+ 披露层真实调度员人工试用。
按任务红线，W-010 维持 queued；基础设施与演练已就绪，门禁一过即可切流。
