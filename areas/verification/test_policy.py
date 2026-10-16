"""W-003 acceptance tests — policy v1 定稿.

Acceptance: 每个权重与 λ 都有来源与支撑度标注，无裸数字； veto 名单已知；
阈值有序；条款映射齐全。
"""
from __future__ import annotations

import pytest

from riskfn import KNOWN_VETO_NAMES, load_policy, list_policy_ids


@pytest.fixture(scope="module")
def policy():
    return load_policy("v1")


def test_policy_v1_loads(policy) -> None:
    assert policy.policy_id == "v1"
    assert policy.effective_from == "2026-11-01"
    assert list_policy_ids() == ["v1"]


def test_no_bare_numbers_every_weight_has_provenance(policy) -> None:
    prov = policy.scoring.provenance["weights_milli"]
    assert set(prov) == set(policy.scoring.weights_milli)
    for name, entry in prov.items():
        assert entry["source"] in {"historical_data", "expert_prior", "legacy_redline"}
        assert isinstance(entry["calibration_support"], int)
        assert entry["calibration_support"] >= 0


def test_veto_types_are_known(policy) -> None:
    assert set(policy.scoring.veto_types) <= set(KNOWN_VETO_NAMES)
    assert "accident" in policy.scoring.veto_types


def test_thresholds_ordered_and_hysteresis(policy) -> None:
    th = policy.executor.thresholds
    assert th["alert"] < th["restrict"] < th["lock"]
    assert th == {"alert": 400, "restrict": 600, "lock": 800}
    assert policy.executor.hysteresis_milli == 50


def test_interaction_whitelist_v1_has_one_calibrated_entry(policy) -> None:
    wl = policy.scoring.interaction_whitelist
    assert len(wl) == 1
    rule = wl[0]
    assert rule.pair == ("telemetry_fatigue_windows", "violation_speeding")
    assert rule.lambda_milli == 80
    assert rule.calibration_support == 3  # 待安全部门复核（D-004）


def test_clause_map_covers_veto_and_levels(policy) -> None:
    cm = policy.clause_map
    for key in (*policy.scoring.veto_types, "alert", "restrict", "lock"):
        assert key in cm


def test_decay_halflives_positive(policy) -> None:
    for carrier, hl in policy.scoring.decay["half_life_days"].items():
        assert hl > 0


def test_loader_rejects_unknown_fact_weight(tmp_path) -> None:
    import json

    from riskfn import PolicyDocument

    doc = json.loads(
        (tmp_path / "policy-v1.json").read_text()
        if False else "{}"
    )
    # build from the real v1 then corrupt one weight name
    import pathlib

    real = json.loads(
        (pathlib.Path(__file__).resolve().parents[2] / "areas/risk-function/policies/policy-v1.json").read_text()
    )
    real["scoring"]["weights_milli"]["not_a_fact"] = 10
    with pytest.raises(Exception, match="unknown fact"):
        PolicyDocument.model_validate(real)
