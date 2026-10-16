"""W-004 unit tests — 纯函数 v0：计算次序、否决优先、先验注入、饱和边界。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from riskfn import (
    FUNCTION_VERSION,
    FactContribution,
    FactEntry,
    RiskContext,
    StateSnapshot,
    load_policy,
    risk,
)

AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
CTX = RiskContext(object_id="veh-1", as_of=AS_OF, policy_id="v1", watermark=10)


@pytest.fixture(scope="module")
def policy():
    return load_policy("v1")


def fact(name: str, freshness: str, contribs=None, **kw) -> FactEntry:
    return FactEntry(
        name=name,
        freshness=freshness,
        contributions=[
            FactContribution(event_id=c[0], value=c[1], age_days=c[2]) for c in (contribs or [])
        ],
        **kw,
    )


def snap(facts: dict, veto_flags=None) -> StateSnapshot:
    return StateSnapshot.build("veh-1", AS_OF, facts, veto_flags or [], watermark_seq=10)


def test_veto_short_circuits_everything(policy) -> None:
    s = snap(
        {
            "violation_accident": fact("violation_accident", "valid", [("ev-acc", 1, 0)]),
            # maximally bad non-veto factors must be skipped
            "violation_speeding": fact("violation_speeding", "valid", [(f"ev-{i}", 99, 0) for i in range(50)]),
            "open_hazard_major": fact("open_hazard_major", "valid", [(f"ev-h{i}", 99, 0) for i in range(50)]),
        },
        veto_flags=["accident"],
    )
    out = risk(s, policy, CTX)
    assert out.r_milli == 1000
    assert out.veto_active is True
    assert out.veto_names == ["accident"]
    assert all(e.kind == "veto" for e in out.explain)
    assert {e.event_id for e in out.explain} == {"ev-acc"}
    assert out.confidence.n_events == 1


def test_no_veto_means_r_below_1000(policy) -> None:
    s = snap(
        {
            "violation_speeding": fact("violation_speeding", "valid", [(f"ev-{i}", 99, 0) for i in range(100)]),
            "open_hazard_major": fact("open_hazard_major", "valid", [(f"eh-{i}", 99, 0) for i in range(100)]),
            "telemetry_fatigue_windows": fact("telemetry_fatigue_windows", "valid", [("et-1", 99, 0)]),
        }
    )
    out = risk(s, policy, CTX)
    assert out.veto_active is False
    assert out.r_milli < 1000


def test_saturation_under_event_storm(policy) -> None:
    facts = {}
    for name in (
        "violation_speeding",
        "violation_fatigue",
        "telemetry_fatigue_windows",
        "telemetry_speeding_windows",
        "open_hazard_major",
        "open_hazard_general",
        "open_work_order_overdue",
    ):
        facts[name] = fact(name, "valid", [(f"{name}-{i}", 500, 0) for i in range(200)])
    out = risk(snap(facts), policy, CTX)
    assert 0 <= out.r_milli <= 1000


def test_prior_injection_when_sensor_missing(policy) -> None:
    base = {
        "telemetry_fatigue_windows": fact("telemetry_fatigue_windows", "missing"),
        "telemetry_speeding_windows": fact("telemetry_speeding_windows", "missing"),
        "violation_speeding": fact("violation_speeding", "valid", [("ev-1", 1, 0)]),
        "lecd_prior": fact("lecd_prior", "valid", [("ex-1", 500, 10)], prior_r_milli=500),
    }
    with_prior = risk(snap(dict(base)), policy, CTX)
    assert with_prior.prior_injected is True
    assert any(e.kind == "prior" for e in with_prior.explain)
    # strength=200 milli → 500 * 200 / 1000 = 100
    assert with_prior.r_milli >= 100

    no_prior = risk(snap({k: v for k, v in base.items() if k != "lecd_prior"}), policy, CTX)
    assert no_prior.prior_injected is False
    assert no_prior.r_milli < with_prior.r_milli


def test_prior_not_injected_when_sensors_valid(policy) -> None:
    s = snap(
        {
            "telemetry_fatigue_windows": fact("telemetry_fatigue_windows", "valid", [("et-1", 2, 1)]),
            "telemetry_speeding_windows": fact("telemetry_speeding_windows", "valid", [("et-2", 1, 1)]),
            "lecd_prior": fact("lecd_prior", "valid", [("ex-1", 900, 10)], prior_r_milli=900),
        }
    )
    out = risk(s, policy, CTX)
    assert out.prior_injected is False


def test_missing_factor_scores_zero_but_coverage_drops(policy) -> None:
    s = snap(
        {
            "violation_speeding": fact("violation_speeding", "valid", [("ev-1", 2, 0)]),
            "open_hazard_major": fact("open_hazard_major", "missing"),
        }
    )
    out = risk(s, policy, CTX)
    assert out.confidence.window_coverage_milli == 500  # 1 of 2 tracked facts valid
    assert out.r_milli == (120 * 1000 // 5 * 2 // 1000)  # weight * g(2/5) // 1000 → 48


def test_expiry_factor_monotone(policy) -> None:
    far = snap({"license_days_remaining": fact("license_days_remaining", "valid", [("rg-1", 0, 5)], days_remaining=59, registry_status="valid")})
    near = snap({"license_days_remaining": fact("license_days_remaining", "valid", [("rg-1", 0, 5)], days_remaining=1, registry_status="valid")})
    beyond = snap({"license_days_remaining": fact("license_days_remaining", "valid", [("rg-1", 0, 5)], days_remaining=61, registry_status="valid")})
    r_far, r_near, r_beyond = (risk(s, policy, CTX).r_milli for s in (far, near, beyond))
    assert r_beyond == 0
    assert r_far < r_near


def test_decay_older_evidence_counts_less(policy) -> None:
    fresh = snap({"violation_speeding": fact("violation_speeding", "valid", [("ev-1", 3, 0)])})
    old = snap({"violation_speeding": fact("violation_speeding", "valid", [("ev-1", 3, 60)])})
    assert risk(fresh, policy, CTX).r_milli > risk(old, policy, CTX).r_milli


def test_explain_sorted_and_top_k(policy) -> None:
    s = snap(
        {
            "violation_speeding": fact("violation_speeding", "valid", [("ev-b", 4, 0), ("ev-a", 4, 0)]),
            "open_hazard_major": fact("open_hazard_major", "valid", [("eh-1", 2, 0)]),
        }
    )
    out = risk(s, policy, CTX)
    assert len(out.explain) <= policy.scoring.explain_top_k
    contribs = [e.contribution_milli for e in out.explain]
    assert contribs == sorted(contribs, reverse=True)
    # ties break by event_id ascending → ev-a before ev-b
    tie = [e for e in out.explain if e.contribution_milli == contribs[0]]
    assert [e.event_id for e in tie] == sorted(e.event_id for e in tie)


def test_golden_replay_byte_identical(policy) -> None:
    s = snap(
        {
            "violation_speeding": fact("violation_speeding", "valid", [("ev-1", 3, 2)]),
            "telemetry_fatigue_windows": fact("telemetry_fatigue_windows", "valid", [("et-1", 2, 1)]),
            "lecd_prior": fact("lecd_prior", "valid", [("ex-1", 216, 30)], prior_r_milli=216),
        }
    )
    first = risk(s, policy, CTX)
    for _ in range(5):
        again = risk(s, policy, CTX)
        assert again.canonical_json() == first.canonical_json()
    assert first.function_version == FUNCTION_VERSION
    assert first.policy_id == "v1"


def test_no_float_types_in_output(policy) -> None:
    s = snap({"violation_speeding": fact("violation_speeding", "valid", [("ev-1", 3, 2)])})
    out = risk(s, policy, CTX)
    assert isinstance(out.r_milli, int)
    assert isinstance(out.confidence.n_events, int)
    assert isinstance(out.confidence.window_coverage_milli, int)
