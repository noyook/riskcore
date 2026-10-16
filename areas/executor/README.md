# executor — 实施说明（W-006 产物）

> 契约文档见 `AGENTS.md` / `predicates.md`；本文件是代码侧导读。

## 布局

```
executor/                     # Python 包（本区域实施代码）
  expressions.py              # 谓词 DSL 安全求值（ast 白名单，禁 eval；
                              # 大写 OR/AND/NOT 归一化为 Python token）
  predicates.py               # Predicate 模型 + PredicateEngine（滞后带 /
                              # fail-closed / 覆写 / R5 条款+证据强制）
  observation.py              # 旁路观察留痕（bypass_observation 表）
  service.py                  # ExecutorService（dispatch hook）+ FastAPI 路由
predicates/                   # 谓词声明（数据非代码）
  allow_dispatch.json         # mode=observe（W-006 旁路；W-010 切 enforce）
  restrict_orders.json
  lock_vehicle.json
  alert_single_fact.json
ddl/sqlite/001_executor.sql   # intervention_state + bypass_observation
```

## 语义要点

- **输入面**（区域规则 1）：只有 RiskOutput（投影器缓存接口）+ 谓词声明 +
  投影器维护的 override 状态。引擎绝不调用纯函数、绝不读日志。
- **阈值全部来自 policy.executor**（alert/restrict/lock + hysteresis_milli），
  谓词只声明用哪条带（`bound`）——执行器不内嵌业务数字。
- **滞后带**：进入 = deny_when 命中且当前未干预；解除 = r 跌破
  （进入带 − hysteresis_milli）；带内维持现状（`hysteresis_hold`）。
  veto_active 命中 → 最高干预，无滞后豁免（区域规则 3）。
- **fail-closed**（区域规则 4）：decision 缺失或 `now - state_as_of >
  stale_after_seconds` → 按 fallback（默认 deny）出 verdict，
  reason_code=stale_fallback。
- **R5 强制**：enforce 态且 intervention≠none 时，clause 或 explain_refs
  缺失 → MissingEvidenceError，拒绝执行（fallback/override 动作除外，
  其 reason_code 自证）。
- **人工覆写**：投影器派生 override_state（最新一条 manual_override 且
  expires_at > as_of）；override 未到期 → 按 override 等级求值、r 不参与，
  verdict.override 带 operator/reason/expires_at 供披露层置顶。
- **旁路观察**（W-006）：所有谓词 mode=observe——会拦截的请求 allow 恒真、
  enforced=false，同时写入 bypass_observation（r、clause、explain_refs），
  作为 W-009 双跑比对语料。W-010 把 allow_dispatch 切 enforce。

## 运行

```bash
# 单部署（ingest + executor + 健康检查 + 投影/对账调度）
python3 scripts/serve.py --db .tmp/riskcore.db --port 8000

# 冒烟（另开终端）
curl -s localhost:8000/health
curl -s -X POST localhost:8000/events -H 'X-Tenant-Id: t1' -H 'Content-Type: application/json' -d @event.json
curl -s -X POST localhost:8000/evaluate -H 'Content-Type: application/json' \
     -d '{"object_id":"veh-1","predicate":"allow_dispatch"}'
```

## 验收记录（2026-10-15）

- 谓词引擎单测 + 属性（迟滞吸收抖动、veto 无豁免、fail-closed、覆写优先、
  覆写到期恢复函数值、表达式沙箱）：`test_executor.py` 20/20。
- 端到端黄金路径（ingest→日志→投影→执行→证据下钻）：`test_integration.py` 4/4。
- 真机冒烟：/health /ready 200；无租户头 401；ingest 分配 seq=1；
  空缓存 evaluate 走 stale_fallback fail-closed。
- 全套件：891 passed / 86 skipped。
- 状态：completed。
