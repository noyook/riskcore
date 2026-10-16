# riskcore — 带证据披露与监管投影的确定性决策函数

> 本体契约见 `AGENTS.md` / `PROJECT.md`；本文件是工程侧总入口。

```
事件 → 事实日志（append-only 本体）→ 投影器（可丢弃缓存 + 对账守护）
     → risk(snapshot, policy_id) 纯函数 → 执行器（滞后/fail-closed/覆写）→ 动作回流
读侧：披露层（对人）/ 投影层（对监管），均不反驱内核。
验证：黄金重放 + 属性测试 + 对账 + PG 权限，CI 门禁。
```

## 快速开始

```bash
# 依赖（Python 3.13；venv 或任意环境）
pip install pydantic pytest fastapi "uvicorn[standard]" httpx jsonschema

# 全套件 + CI 门禁（黄金重放 + 属性测试 + 对账 + PG 权限）
bash scripts/verify_all.sh

# 单部署服务（ingest + executor + disclosure + projection + 投影/对账调度）
python3 scripts/serve.py --db .tmp/riskcore.db --port 8000
```

## 区域 → 代码地图

| 区域 | 实施代码 | 契约文档 |
|---|---|---|
| event-log | `areas/event-log/eventlog/`（envelope/store/relay/ingest_api/projector/reconciler）+ `ddl/` | `event-schema.md` `projector.md` |
| risk-function | `areas/risk-function/riskfn/`（function/decay/factors/snapshot/policy/facts）+ `policies/` | `function-contract.md` `policy-format.md` |
| executor | `areas/executor/executor/`（expressions/predicates/observation/service）+ `predicates/` + `ddl/` | `predicates.md` |
| disclosure | `areas/disclosure/disclosure/`（explain + `static/index.html`） | `explain-api.md` |
| projection | `areas/projection/projection/`（stamp/fourcolor/lecd/doc6/ledger/service）+ `ddl/` | `projections.md` |
| verification | `areas/verification/`（factories/测试套件/golden/replay/duality） | `test-plan.md` |

各区域的实施说明（布局/语义/验收记录）见对应 `areas/<area>/README.md`。

## 关键不变量（机器保障）

| 不变量 | 保障 |
|---|---|
| 日志只追加 | PG GRANT（UPDATE/DELETE/TRUNCATE 均拒）+ SQLite 触发器；`verify_pg_perms.sh` 真机验证 |
| 黄金重放逐比特一致 | `areas/verification/replay.py`，6 夹具；纳入 CI 门禁 |
| 属性（无否决 r<1000 / 否决优先 / 饱和 / 单调 / 迟滞 / correction / 覆写衰减） | `test_properties.py` + 各层单测，908 passed |
| 缓存不是真相 | reconciler 漂移检出+重算修正；CI 对账漂移=0 |
| 无裸数字 | policy loader 强制 provenance；权重只引用 facts.py 词表 |
| 披露只读 + 覆写置顶 | ExplainService 无写路径；override 组 banner 恒置顶 |

## HTTP 端点（单部署）

| 端点 | 区域 | 说明 |
|---|---|---|
| `POST /events` `/events:batch` | event-log | 唯一写入口；`X-Tenant-Id` 强制（401）；seq 服务端分配 |
| `POST /evaluate` | executor | 谓词求值（旁路期 allow 恒真 + 留痕） |
| `GET /explain?object_id=` | disclosure | 证据链全量协议 |
| `GET /ui/` | disclosure | 调度端界面 |
| `GET /projections/{kind}` `POST /projections/regenerate` | projection | 四色/6号文/LEC-D/台账；落盘 `outputs/projections/` |
| `GET /health` `/ready` | 共享 | 存活/就绪（store 不可达 → 503） |

## 任务状态

W-001 ~ W-008 completed；W-009 框架就位（真机双跑待现网快照授权）；
W-010 queued（披露层人工试用 + 双跑为前置红线）。详见 `STATUS.md`。
