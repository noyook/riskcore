# Policy 文档格式与变更流程

## 结构（版本化 JSON）

```json
{
  "policy_id": "v1",
  "effective_from": "2026-11-01",
  "scoring": {
    "veto_types": ["accident", "major_hazard_open", "tank_inspection_overdue", "license_invalid"],
    "weights_milli": { "violation": 120, "telemetry_fatigue": 100, "open_hazard": 150 },
    "decay": { "half_life_days": { "violation": 30, "telemetry": 7 } },
    "factor_params": { "days_to_license_expiry": { "type": "inverse_linear", "floor": 0, "cap": 1000 } },
    "interaction_whitelist": [
      { "pair": ["telemetry_fatigue", "violation_speeding"], "lambda_milli": 80, "calibration_support": 3 }
    ],
    "prior_injection": { "strength_milli": 200, "assertion_ttl_days": 365 },
    "missing_factor": "ignore_with_coverage_penalty",
    "explain_top_k": 5
  },
  "executor": {
    "thresholds": { "alert": 400, "restrict": 600, "lock": 800 },
    "hysteresis_milli": 50,
    "manual_override_ttl_hours": 72
  },
  "clause_map": { "tank_inspection_overdue": "6号文-第X条", "lock": "6号文-第Y条" }
}
```

## 变更流程

1. 起草新版本（拷贝上一版，改差异）→ 走评审 → `effective_from` 生效。
2. 旧版本永久保留，不可修改；历史输出用当时的 policy_id 可精确重放。
3. 任何版本变更写入 DECISIONS.md。
4. 双跑期：新旧 policy 并行计算，差异报告驱动参数标定。

## 参数标定纪律

- 权重/λ 优先由历史事故数据派生；样本不足时允许专家给定，但必须在 `calibration_support` 如实标注。
- 禁止为了拟合个别案例调权重——那是过拟合，不是标定。
