# 旧快照数据标记方案（W-002 交付物 3）

> 结论先行：**迁移不新建任何"当前值库"类表**。旧快照数据只做标记与冻结，
> 不做同步、不做双写。本体只有一个：事实日志。

## 1. 为什么不再建"当前值库"

D-001（日志为唯一本体，状态库为可丢弃投影）与 D-010（静态线/动态线合一）已从
架构上取消"当前值库"的独立地位：

- 年度辨识（原静态线）→ `expert_assertion` 事件，变率慢、有效期长；
- 遥测/违法（原动态线）→ 快变事件；
- 同一日志，语义由事件自带；年度修订 = 追加新断言 + 旧断言被 supersedes
  （correction 链）。

旧系统的"当前值库"表（KRI 当前值、风险码当前态）在双跑期只作为 **legacy
只读快照源**（STATUS Q3），不进入本内核的任何写路径。

## 2. 旧快照的处置（标记 → 冻结 → 只读引用）

| 步骤 | 动作 | 落点 |
|---|---|---|
| 标记 | 旧库全部表加注释 `legacy-snapshot: frozen <date>`；应用账号收回写权限，只留 SELECT | 旧库 DBA 执行 |
| 冻结 | 停止一切增量同步/双写；冻结日全量快照另存为只读副本 | 旧库 |
| 引用 | 双跑比对经 `LegacySource` 适配器只读消费（areas/verification/duality.py） | 内核外 |
| 断代 | 双跑结束（W-009 完成）后旧库下线，快照副本入 archive/ | archive/ |

## 3. 本仓库内的落地形式

- **迁移数据**全部以事件形式进入日志（`expert_assertion`，provenance 标注
  `historical-migration`），无旁路表；
- 迁移台账（批次、行数、对账）**不从日志派生**：`trace_id=migrate-<batch_id>`
  即批次键，`GET /migrate/report?batch_id=…` 随时从日志重建；
- 若未来确需记录"某旧快照已于某日被封存"，按投影层规则追加
  `projection_only` 事件（material_kind=`legacy_snapshot_frozen`），**不建表**。

## 4. 验收对应（W-002）

- "任一原纸质表条目可按 object_id 在日志中检索到"：灌入报告每行带 object_id +
  event_id + seq，披露层 `/ui/?object=<object_id>` 直接可查；
- "迁移过程产生 correction 记录的比例报告"：灌入报告输出
  `correction_ratio_milli`（修正事件 / 提交行数）。
