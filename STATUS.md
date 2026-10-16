# STATUS.md — 当前状态与下一步

> 更新纪律：每次推进后同步本文件；只写事实与下一步，不写感想。

## 当前状态（M0–M3 骨架与内核完成，M4 框架就位待数据）

- [x] 项目骨架与全局规则定稿（AGENTS.md / PROJECT.md / DECISIONS.md）
- [x] 六大区域划分与区域规则就位（areas/*/AGENTS.md）
- [x] W-001 事件信封与日志表（PG15 真机权限验证 8/8；schema v1.1 增补 registry）
- [x] W-002 存量数据灌入（可视化录入界面 /migrate-ui/ + 幂等灌入 + 对账报告 + 旧快照标记方案，D-016）
- [x] W-003 policy v1 定稿（专家先验版，无裸数字，D-013）
- [x] W-004 纯函数 v0（veto→先验→单因子→交互项接口→饱和→explain）
- [x] W-005 投影器与对账守护（漂移检出/修正/留痕，全量重放=增量投影）
- [x] W-006 执行器与 allow_dispatch 旁路（谓词 DSL 沙箱、滞后带、fail-closed、R5）
- [x] W-007 证据披露 API + 调度端界面（/explain 全量协议 + /ui/）
- [x] W-008 四色视图 + 监管投影三件套（钢印 + 幂等重生成）
- [x] W-009 框架+流水线（黄金重放 6/6 + CI 门禁 GREEN + 落盘区适配器 + 每日双跑器 + 5 天模拟排练：0 不可解释）—— 真机 14 天待 Q3 授权
- [ ] W-010 旁路转 fail-closed —— 保持 queued（基础设施+演练已就绪：enforce 拦截验证、回滚 70 秒；红线：W-007 人工试用 + W-009 14 天）

## 验证体系状态

- 全套件：**923 passed / 86 skipped**（`python3 -m pytest areas/verification/`）
- CI 门禁：`scripts/verify_all.sh` = 黄金重放 + 属性测试 + 对账漂移=0 + PG 权限验证，
  当前 **GATE GREEN**（PG 步骤需在 CI 配置 PGHOST 后真机执行）
- 黄金重放：6 个固定夹具逐比特一致（veto/mid/clean/prior-only/correction/hazard-closed）

## 下一步

1. ~~**W-002**~~ 已完成：界面 /migrate-ui/ 供企业用户直接录入；批量电子版可走粘贴导入。
2. **W-009**：现网 risk_code_assignments 快照授权（Q3）→ 放入 `.tmp/legacy_snapshots/<日期>.csv` → `python3 scripts/dualrun.py --day <日期>` 每日跑，满 14 天。
3. **W-007 人工验收**：真实调度员用 `/ui/trial.html` 做 30 秒证据链试用并打分
   （工具已就绪：计时+六项评分+自动判定+台账）→ ≥3 人通过后解冻 W-010。
4. **函数 v1.1**：交互项求值（白名单已就位）、release 条件的工单闭环门控。
5. **Q4**：legacy 5 条锁车红线与 veto_types 逐条对应关系（安全部门确认）。

## 待决项（Open Questions）

| # | 问题 | 阻塞谁 | 倾向 |
|---|---|---|---|
| Q1 | 日志存储选型 | W-001 | **已闭合**：PG15 生产 + SQLite 开发双轨（D-012） |
| Q2 | 函数语言与整数约定 | W-004 | **已闭合**：Python 3.13 + r 毫单位整数（D-011） |
| Q3 | 双跑比对的旧系统数据源口径 | W-009 | 取现网 risk_code_assignments 每日快照（待授权） |
| Q4 | legacy 5 条锁车红线 ↔ veto_types 逐条对应 | W-003 附录 | 安全部门确认后更新 clause_map 与 DECISIONS |

## 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 事故样本个位数，交互项系数过拟合 | 高 | 白名单制 + 披露层标注"未经充分校准"（已落地：calibration_notes） |
| 快照漂移无人发现 | 中 | 对账守护进程 + 漂移告警（已落地：reconciler，CI 漂移=0 门禁） |
| 调度/司机对自动裁判不信任 | 高 | 披露层已上线（W-007），人工试用验收为 W-010 前置门禁 |
| 先验权重未经数据校准 | 高 | D-013 如实标注；双跑差异报告驱动 v2 标定 |
