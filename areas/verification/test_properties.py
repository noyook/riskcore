"""Property-based suite — function-level invariants (verification/test-plan.md §2).

Seeded PRNG only (random.Random(seed)); no wall clock anywhere. Executor- and
projector-level invariants (迟滞在决策层 / correction / 覆写衰减) live in
test_executor.py / test_projector.py and run once those modules land.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from riskfn import FactContribution, FactEntry, RiskContext, StateSnapshot, load_policy, risk

AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

COUNT_FACT_NAMES = [
    "violation_speeding",
    "violation_fatigue",
    "violation_accident",
    "telemetry_fatigue_windows",
    "telemetry_speeding_windows",
    "open_hazard_major",
    "open_hazard_general",
    "open_work_order_overdue",
]
EXPIRY_FACT_NAMES = ["license_days_remaining", "tank_inspection_days_remaining"]
ALL_FACT_NAMES = COUNT_FACT_NAMES + EXPIRY_FACT_NAMES + ["lecd_prior"]


def random_snapshot(rng: random.Random, *, allow_veto: bool = False) -> StateSnapshot:
    facts: dict[str, FactEntry] = {}
    veto_flags: list[str] = []
    for name in ALL_FACT_NAMES:
        freshness = rng.choice(["valid", "valid", "valid", "stale", "missing"])
        contribs = []
        if freshness != "missing" and rng.random() < 0.9:
            n = rng.randint(1, 8)
            for i in range(n):
                contribs.append(
                    FactContribution(
                        event_id=f"{name}-ev{i}-{rng.randrange(1 << 30)}",
                        value=rng.choice([0, 1, 2, 3, 5, 9, 40, 500]),
                        age_days=rng.choice([0, 0, 1, 3, 7, 15, 30, 90, 400]),
                    )
                )
        entry = FactEntry(name=name, freshness=freshness, contributions=contribs)
        if name in EXPIRY_FACT_NAMES and freshness != "missing":
            entry.days_remaining = rng.choice([-5, 0, 1, 5, 20, 45, 90, 200])
            entry.registry_status = rng.choice(["valid", "valid", "valid", "expired", "revoked"])
            # projector would veto on status!=valid or days<=0 — model that
            if allow_veto and (entry.registry_status != "valid" or entry.days_remaining <= 0):
                veto = "license_invalid" if name.startswith("license") else "tank_inspection_overdue"
                if veto not in veto_flags:
                    veto_flags.append(veto)
        if name == "lecd_prior" and freshness != "missing":
            entry.prior_r_milli = rng.choice([27, 125, 500, 999])
        if name == "violation_accident" and allow_veto and contribs and rng.random() < 0.3:
            if "accident" not in veto_flags:
                veto_flags.append("accident")
        if name == "open_hazard_major" and allow_veto and contribs and rng.random() < 0.3:
            if "major_hazard_open" not in veto_flags:
                veto_flags.append("major_hazard_open")
        if name == "violation_accident" and allow_veto and "accident" in veto_flags and not contribs:
            # projector contract: a veto flag is always backed by its triggering event
            entry.contributions = [FactContribution(event_id="acc-evid", value=1, age_days=0)]
            entry.freshness = "valid"
        if name == "open_hazard_major" and allow_veto and "major_hazard_open" in veto_flags and not contribs:
            entry.contributions = [FactContribution(event_id="haz-evid", value=1, age_days=0)]
            entry.freshness = "valid"
        if name in EXPIRY_FACT_NAMES and allow_veto and not contribs and freshness != "missing":
            if (name.startswith("license") and "license_invalid" in veto_flags) or (
                name.startswith("tank") and "tank_inspection_overdue" in veto_flags
            ):
                entry.contributions = [FactContribution(event_id="reg-evid", value=0, age_days=0)]
        facts[name] = entry
    return StateSnapshot.build("obj-x", AS_OF, facts, veto_flags, watermark_seq=rng.randrange(1000))


@pytest.fixture(scope="module")
def policy():
    return load_policy("v1")


CTX = RiskContext(object_id="obj-x", as_of=AS_OF, policy_id="v1", watermark=7)


@pytest.mark.parametrize("seed", range(200))
def test_no_veto_implies_r_below_1000(policy, seed: int) -> None:
    rng = random.Random(seed)
    s = random_snapshot(rng, allow_veto=False)
    out = risk(s, policy, CTX)
    assert out.veto_active is False
    assert out.r_milli < 1000


@pytest.mark.parametrize("seed", range(200))
def test_veto_priority_and_full_short_circuit(policy, seed: int) -> None:
    rng = random.Random(10_000 + seed)
    s = random_snapshot(rng, allow_veto=True)
    if not s.veto_flags:
        pytest.skip("seed produced no veto")
    out = risk(s, policy, CTX)
    assert out.veto_active is True
    assert out.r_milli == 1000
    assert sorted(out.veto_names) == s.veto_flags
    # 跳过后续所有步骤：explain 只含 veto 证据，无 factor/prior 条目
    assert out.explain and all(e.kind == "veto" for e in out.explain)


@pytest.mark.parametrize("seed", range(200))
def test_saturation_event_storm(policy, seed: int) -> None:
    rng = random.Random(20_000 + seed)
    s = random_snapshot(rng, allow_veto=False)
    out = risk(s, policy, CTX)
    assert 0 <= out.r_milli <= 1000


@pytest.mark.parametrize("seed", range(100))
def test_golden_replay_property(policy, seed: int) -> None:
    rng = random.Random(30_000 + seed)
    s = random_snapshot(rng, allow_veto=True)
    first = risk(s, policy, CTX)
    second = risk(s, policy, CTX)
    assert first.canonical_json() == second.canonical_json()


@pytest.mark.parametrize("seed", range(200))
def test_monotone_mapping(policy, seed: int) -> None:
    """单个危险 fact 恶化（其他不变）→ r 不下降。"""
    rng = random.Random(40_000 + seed)
    base = random_snapshot(rng, allow_veto=False)

    name = rng.choice(COUNT_FACT_NAMES + EXPIRY_FACT_NAMES)
    if name == "violation_accident":
        pytest.skip("accident is a veto carrier, not a scored factor")

    worse_facts = {k: v.model_copy(deep=True) for k, v in base.facts.items()}
    entry = worse_facts[name]
    if name in EXPIRY_FACT_NAMES:
        base_days = entry.days_remaining if entry.days_remaining is not None else 90
        entry.days_remaining = base_days - 30
        if entry.days_remaining <= 0:
            pytest.skip("would flip into veto territory")
    else:
        # add one more, fresher, larger contribution
        entry.contributions = list(entry.contributions) + [
            FactContribution(event_id=f"{name}-worse-{rng.randrange(1 << 30)}", value=50, age_days=0)
        ]
        entry.freshness = "valid"
    worse = StateSnapshot.build("obj-x", AS_OF, worse_facts, base.veto_flags, base.watermark_seq)

    r0 = risk(base, policy, CTX).r_milli
    r1 = risk(worse, policy, CTX).r_milli
    assert r1 >= r0, f"fact {name} worsened but r dropped {r0} -> {r1}"
