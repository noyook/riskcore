# event-log — 实施说明（W-001 产物）

> 契约文档见 `AGENTS.md` / `event-schema.md` / `projector.md`；本文件是代码侧导读。

## 布局

```
eventlog/                  # Python 包（本区域实施代码）
  types.py                 # EventType / ObjectType 字典（与 event-schema.md 同源）
  envelope.py              # 信封 Pydantic 模型 = 可执行契约（payload 按类型强校验）
  store.py                 # Append-only 存储接口 + SQLite 开发态实现
  relay.py                 # outbox 中继（at-least-once，消费端按 event_id 幂等）
  ingest_api.py            # 唯一写入口：POST /events（租户头强制、seq 服务端分配）
  app.py                   # 单部署外壳（D-005）：ingest 路由 + /health + /ready
  projector.py             # 状态投影器：事件 → fact 快照 + 决策缓存 + 水位线（W-005）
  reconciler.py            # 对账守护：抽样/全量重算、漂移检出、以重算修正（W-005）
ddl/postgres/001_event_log.sql   # 生产 DDL：LIST 分区、GRANT 只读追加、correction 约束触发器
ddl/postgres/002_projection.sql  # 投影缓存表 DDL（+ riskcore_projector 角色）
ddl/sqlite/001_event_log.sql     # 开发/测试 Schema：触发器强制只追加
ddl/sqlite/002_projection.sql    # 投影缓存表 Schema
schemas/event-envelope.schema.json  # 由 Pydantic 模型生成（scripts/gen_schema.py）
```

## 投影器语义（W-005）

- **as_of 外置**：投影器体内不读时钟，`project_object(object_id, as_of)` 的
  as_of 一律由调用方（调度器/测试）注入——铁律一对投影体同样生效。
- **correction 先行**：派生 fact 前先应用修正链（最新一条 correction 的载荷
  替换原事件载荷，原事件与 provenance 永久不动）；修正后输出与"原事件从未
  发生"一致（属性测试锁定）。
- **veto 派生**：accident（近期，违法窗口内）/ major_hazard_open（未闭环重大
  隐患）/ license_invalid、tank_inspection_overdue（registry 最新事件状态）。
- **新鲜度**：窗口 = 载体半衰期（policy.decay.half_life_days）；超窗 → stale，
  无数据 → missing；先验事实的新鲜度由断言有效期决定。
- **全量重算**：每次投影都对对象全事件流重算（幂等、可重放）。projector.md
  的"增量维护"是优化项，正确性优先；对账守护是安全网。
- **对账守护**：`reconcile_object` 先读旧缓存 → 全量重算 → 比对 → 有漂移则以
  重算修正并追加 `projection_only`（material_kind=reconciliation_run）留痕；
  抽样选择确定性（排序 + 种子）。

## 关键决策（已留痕 DECISIONS.md）

- **D-011**：实现语言 Python 3.13；`r` 以 0–1000 整数毫单位表示（禁浮点）。
- **D-012**：存储双轨——生产 PostgreSQL 15 分区表（Q1 结论），开发/测试/重放用
  SQLite，二者共用同一存储接口；内核永不直接依赖存储。
- seq 由 `object_seq` 计数器表在写事务内分配（`BEGIN IMMEDIATE` 串行化）；
  `object_seq` 是序列元数据、不是日志本体，因此允许 UPDATE。
- 迟到事件（`ingested_at - occurred_at` 超阈值）正常入日志拿新 seq；
  watermark 只前进，由投影器负责（见 `projector.py`，W-005）。

## 运行

```bash
# 单元/验收测试（含 append-only、correction 链、迟到事件、ingest 边界）
python3 -m pytest areas/verification/test_event_log.py

# 重新生成信封 JSON Schema
python3 scripts/gen_schema.py

# PostgreSQL 真机权限验证（append-only 验收）：
createdb riskcore_test
psql -d riskcore_test -f areas/event-log/ddl/postgres/001_event_log.sql
PGUSER=riskcore_app scripts/verify_pg_perms.sh
```

## 验收记录（2026-10-15）

| 验收项 | 证据 |
|---|---|
| 无法 UPDATE/DELETE 日志表 | PG 15 真机：`permission denied for table event_log`（UPDATE/DELETE/TRUNCATE 均拒）；SQLite 触发器拒写 |
| correction 指向不存在 event_id 被拒 | PG 约束触发器 `foreign_key_violation`；SQLite 单测 `CorrectionTargetMissing` |
| 迟到事件可入日志且不影响 watermark | `test_late_event_enters_log_without_moving_watermark` 通过 |
| 幂等写入 / seq 对象内单调 / outbox 同事务 | `test_event_log.py` 15/15 通过 |
