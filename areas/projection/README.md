# projection — 实施说明（W-008 产物）

> 契约文档见 `AGENTS.md` / `projections.md`；本文件是代码侧导读。

## 布局

```
projection/
  stamp.py               # 版本钢印三件套 + 幂等校验助手
  fourcolor.py           # 四色风险分布（阈值取自 policy.executor，映射是数据）
  lecd.py                # 《年度风险辨识评估报告》生成器 + Markdown 渲染
  doc6.py                # 6号文红黄码清单（谓词 clause + 干预状态）
  ledger.py              # 双重预防闭环台账（工单事件流 + 闭环状态）
  service.py             # ProjectionService：四个产物 + 重生成 SOP + 路由
ddl/postgres/003_projections.sql   # 审计用 SQL 视图（四色分布 / 双预台账）
```

## 规则落地

- **钢印**（区域规则 1）：每份产物 stamp = function_version + policy_id +
  as_of + regenerated_at；`assert_stamped` 缺一即抛错。
- **口径变更只动投影**（区域规则 2）：四色阈值来自 `policy.executor`，
  监管对齐改 policy 或映射表，内核 schema 与函数不动。
- **重生成幂等**（区域规则 4）：同输入两次生成，除 `regenerated_at` 外
  逐比特一致（`test_regeneration_is_idempotent` 锁定）。
- **旧版归档不覆盖**：产物路径携带 policy_id + as_of；口径变更自然落到
  新文件，旧文件留在 `outputs/projections/<kind>/`。

## 运行

```bash
# 单部署内：
curl "localhost:8000/projections/four_color"
curl "localhost:8000/projections/doc6_list"
curl -X POST "localhost:8000/projections/regenerate"   # 落盘 outputs/projections/
```

## 验收记录（2026-10-15）

- `test_projection.py` 6/6：钢印齐全、四色映射（veto→红/干净→蓝）、
  LEC-D 章节与 D 值计算 + Markdown 页脚钢印、6号文红黄码、台账
  open/overdue/closed、重生成幂等（除 regenerated_at 外一致）。
- 全套件：904 passed / 86 skipped。
- 状态：completed。
