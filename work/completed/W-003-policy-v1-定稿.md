# W-003 policy v1 定稿

- 状态：queued ｜ 里程碑：M0 ｜ 依赖：无（可与 W-001 并行）

## 目标
按 areas/risk-function/policy-format.md 产出第一版政策文档。

## 产出
1. policy v1 JSON：否决清单（移植现有 5 条锁车红线）、三类核心因子权重（证照/隐患/违法）、衰减半衰期、交互项白名单 v1（疲劳×超速）、先验注入参数。
2. 每条权重的来源标注（历史数据 / 专家给定 + calibration_support）。
3. clause_map（条款号映射表）。

## 验收
- 每个权重与 λ 都有来源与支撑度标注，无裸数字。
- 通过评审并写入 DECISIONS.md。

---

## 验收记录（2026-10-15）

- 产出：`areas/risk-function/policies/policy-v1.json`、`riskfn/policy.py`（加载器+校验器）、
  `riskfn/facts.py`（事实字典：权重引用词表，loader 强制）。
- 校验器强制：权重∈[0,1000] 且引用已知 fact、每个权重/λ 有 provenance（source+calibration_support）、
  veto 名单已知、阈值有序、clause_map 覆盖 veto 与三档干预线。
- 测试：`areas/verification/test_policy.py` 8/8 通过。
- 决策留痕：D-013（v1 为专家先验版）；条款号占位、legacy 红线对应关系列为 STATUS 待决 Q4。
- 状态：completed。
