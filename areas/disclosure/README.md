# disclosure — 实施说明（W-007 产物）

> 契约文档见 `AGENTS.md` / `explain-api.md`；本文件是代码侧导读。

## 布局

```
disclosure/
  explain.py               # ExplainService：GET /explain 全量协议构建器（只读）
  static/index.html        # 调度端最小可用界面（原生 JS，无构建依赖）
```

## 协议要点（explain-api.md 的实现映射）

- `current`：r_milli / veto_active / policy_id / state_as_of（来自决策缓存）。
- `conclusion`：一句话业务语言。覆写生效时置顶且**底层状态如实并列**
  （"人工调整中：解除覆写…；底层状态：强制条款命中：罐检逾期"）——D-009：
  覆写不遮遮掩掩。
- `confidence`：n_events / window_coverage_milli / data_freshness_milli +
  calibration_notes（交互项支撑样本 <10 → "未经充分校准"标注，D-004）。
  `low_confidence`（覆盖率或新鲜度 <500）驱动界面视觉降级。
- `evidence_chain`：分组 veto / prior / factors（interaction 在函数 v1.1 开），
  每条证据带 plain_text（业务语言）+ occurred_at + provenance + 下钻 event_id。
  覆写组 banner=true 且恒置顶（区域规则 3）。
- `active_interventions`：干预卡 = predicate + clause + release_condition
  （"r < 750 且相关整改工单闭环"——800−50 滞后带的业务语言）。

## 只读纪律（区域规则 1）

本服务没有任何写路径：数据只经 日志读接口 / 投影器读接口 / 执行器只读干预
查询取得。人工覆写入口不在本区域——覆写是 manual_override 事件，经
event-log 区域的 POST /events 写入（审批流外部化，PROJECT.md 非目标：
谁干/顺序/催办用现成任务路由）。

## 运行

```bash
python3 scripts/serve.py --db .tmp/riskcore.db --port 8000
# 调度端界面: http://127.0.0.1:8000/ui/
# 证据链 API: curl "http://127.0.0.1:8000/explain?object_id=veh-demo"
```

## 验收记录（2026-10-15）

- `test_disclosure.py` 7/7：协议形状、否决证据组、覆写横幅置顶、干预卡
  解除条件业务语言、低置信降级 + 校准标注、先验注入独立分组、未知对象
  fail-closed 提示。
- 真机冒烟：ingest（violation+registry 罐检过期）→ 投影 tick → /explain
  返回 veto 证据 + "已锁定：罐检逾期"结论；/evaluate 后干预卡出现；
  manual_override(clear) → 横幅置顶 + 结论改"人工调整中"+ evaluate 走
  override_clear（r 不参与）；/ui/ 200。
- 人工试用验收（真实调度员 30 秒出示证据链）留待真人，见 STATUS.md。
- 状态：completed（代码与接口验收；人工试用为上线前检查项）。
