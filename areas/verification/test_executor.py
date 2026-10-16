"""W-006 executor tests — 谓词引擎 / 滞后带 / fail-closed / 覆写 / 旁路观察。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from executor import (
    ExpressionError,
    ExecutorService,
    HysteresisState,
    MissingEvidenceError,
    Predicate,
    PredicateEngine,
    Thresholds,
    eval_bool,
    load_predicates,
)
from executor.observation import BypassObservation
from executor.service import SqliteStateStore
from riskfn import load_policy

AS_OF = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
NOW = AS_OF + timedelta(minutes=1)


class FakeDecision:
    def __init__(self, r_milli: int, veto_active: bool = False, as_of: datetime = NOW, explain=None, object_id: str = "obj-1"):
        self.r_milli = r_milli
        self.veto_active = veto_active
        self.as_of = as_of
        self.explain = explain or []
        self.object_id = object_id


class FakeOverride:
    def __init__(self, level="restrict", expires_at=None):
        self.level = level
        self.operator = "dispatcher-1"
        self.approver = "supervisor-1"
        self.reason = "manual review"
        self.expires_at = expires_at or (NOW + timedelta(hours=72))
        self.event_id = "ov-1"


@pytest.fixture(scope="module")
def policy():
    return load_policy("v1")


@pytest.fixture
def engine(policy):
    return PredicateEngine(
        Thresholds(
            alert=policy.executor.thresholds["alert"],
            restrict=policy.executor.thresholds["restrict"],
            lock=policy.executor.thresholds["lock"],
            hysteresis_milli=policy.executor.hysteresis_milli,
        )
    )


@pytest.fixture
def predicates():
    return load_predicates()


class FakeProjector:
    def __init__(self, decision=None, override=None):
        self._decision = decision
        self._override = override

    def read_decision(self, object_id):
        return self._decision

    def read_override(self, object_id):
        return self._override


@pytest.fixture
def service(policy, tmp_path):
    return ExecutorService(FakeProjector(), policy, state_db_path=str(tmp_path / "executor.db"))


# --------------------------------------------------------- expression safety


def test_expression_eval_basic() -> None:
    ns = {"veto_active": False, "r_milli": 620, "lock_threshold": 800, "restrict_threshold": 600}
    assert eval_bool("veto_active OR r_milli >= lock_threshold", ns) is False
    assert eval_bool("r_milli >= restrict_threshold", ns) is True
    assert eval_bool("NOT veto_active AND r_milli >= restrict_threshold", ns) is True


def test_expression_eval_rejects_code_execution() -> None:
    ns = {"r_milli": 1}
    for evil in (
        "__import__('os').system('echo pwned')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "r_milli.__class__",
        "[x for x in range(10)]",
        "lambda: 1",
    ):
        with pytest.raises(ExpressionError):
            eval_bool(evil, ns)


def test_expression_eval_rejects_unknown_names() -> None:
    with pytest.raises(ExpressionError, match="unknown name"):
        eval_bool("not_a_name > 1", {"r_milli": 1})


# ------------------------------------------------------------- predicate v1


def test_builtin_predicates_load(predicates) -> None:
    assert set(predicates) == {"allow_dispatch", "restrict_orders", "lock_vehicle", "alert_single_fact"}
    ad = predicates["allow_dispatch"]
    assert ad.mode == "observe"  # W-006 旁路观察期
    assert ad.fallback == "deny"
    assert ad.clause  # R5 条款必备


# -------------------------------------------------------------- bypass mode


def test_allow_dispatch_observe_mode_lets_deny_through(service) -> None:
    decision = FakeDecision(r_milli=900, explain=["ev-1"])
    service.projector._decision = decision
    v = service.evaluate("obj-1", "allow_dispatch", NOW)
    assert v.intervention == "lock" and v.allow is True and v.enforced is False
    assert v.reason_code == "entered"
    assert v.clause and v.explain_refs == ["ev-1"]
    # 旁路留痕
    assert service.observations.count() == 1
    obs = service.observations.list()[0]
    assert obs.object_id == "obj-1" and obs.would_intervention == "lock"


def test_enforce_mode_denies(service, predicates) -> None:
    predicates["allow_dispatch"].mode = "enforce"
    service.predicates = predicates
    service.projector._decision = FakeDecision(r_milli=900, explain=["ev-1"])
    v = service.evaluate("obj-1", "allow_dispatch", NOW)
    assert v.allow is False and v.enforced is True
    assert service.observations.count() == 0


def test_missing_evidence_refuses_to_act(service, predicates) -> None:
    predicates["allow_dispatch"].mode = "enforce"
    service.predicates = predicates
    service.projector._decision = FakeDecision(r_milli=900, explain=[])  # 无证据
    with pytest.raises(MissingEvidenceError):
        service.evaluate("obj-1", "allow_dispatch", NOW)


# ------------------------------------------------------------------ hysteresis


def _eval_seq(engine, predicate, rs, veto=False):
    """Evaluate a sequence of r values; returns verdicts + final state."""
    verdicts = []
    state = None
    for i, r in enumerate(rs):
        decision = FakeDecision(r_milli=r, veto_active=veto, as_of=AS_OF + timedelta(minutes=i))
        state = state or HysteresisState(intervention="none", r_at_entry=None, since=None)
        v = engine.evaluate(predicate, decision, None, state, AS_OF + timedelta(minutes=i))
        state = HysteresisState(intervention=v.intervention, r_at_entry=v.r_milli, since=None)
        verdicts.append(v)
    return verdicts


def test_hysteresis_enter_hold_release(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    verdicts = _eval_seq(engine, p, [500, 800, 780, 749, 550])
    assert [v.intervention for v in verdicts] == ["none", "lock", "lock", "none", "none"]
    assert [v.reason_code for v in verdicts] == ["ok", "entered", "hysteresis_hold", "released", "ok"]


def test_hysteresis_band_absorbs_jitter(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    verdicts = _eval_seq(engine, p, [800, 760, 790, 755, 748])
    assert [v.reason_code for v in verdicts] == ["entered", "hysteresis_hold", "hysteresis_hold", "hysteresis_hold", "released"]


def test_veto_has_no_hysteresis_exemption(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    # 即使 r 很低，veto 立即最高干预
    v = engine.evaluate(p, FakeDecision(r_milli=10, veto_active=True), None,
                        HysteresisState("none", None, None), NOW)
    assert v.intervention == "lock" and v.reason_code == "veto_active"
    # veto 清除后回到带内 → 维持（不立即解除）
    v2 = engine.evaluate(p, FakeDecision(r_milli=780), None,
                         HysteresisState("lock", 10, None), NOW)
    assert v2.intervention == "lock" and v2.reason_code == "hysteresis_hold"


# --------------------------------------------------------------- fail-closed


def test_stale_decision_triggers_fallback(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    old = FakeDecision(r_milli=100, as_of=NOW - timedelta(minutes=30))  # stale_after=15m
    v = engine.evaluate(p, old, None, HysteresisState("none", None, None), NOW)
    # fail-closed：取不到可信 r → 按最严干预出 verdict（旁路期 allow 恒真但不拦截）
    assert v.intervention == "lock" and v.reason_code == "stale_fallback"


def test_stale_fallback_denies_in_enforce_mode(engine, predicates) -> None:
    p = predicates["allow_dispatch"].model_copy(update={"mode": "enforce"})
    old = FakeDecision(r_milli=100, as_of=NOW - timedelta(minutes=30))
    v = engine.evaluate(p, old, None, HysteresisState("none", None, None), NOW)
    assert v.intervention == "lock" and v.reason_code == "stale_fallback" and v.allow is False


def test_missing_decision_triggers_fallback(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    v = engine.evaluate(p, None, None, HysteresisState("none", None, None), NOW)
    assert v.intervention == "lock" and v.reason_code == "stale_fallback"


def test_fresh_decision_not_stale(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    fresh = FakeDecision(r_milli=100, as_of=NOW - timedelta(minutes=5))
    v = engine.evaluate(p, fresh, None, HysteresisState("none", None, None), NOW)
    assert v.intervention == "none" and v.reason_code == "ok"


# ------------------------------------------------------------------- override


def test_override_takes_priority_over_r(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    v = engine.evaluate(p, FakeDecision(r_milli=900, explain=["ev-1"]), FakeOverride(level="restrict"),
                        HysteresisState("none", None, None), NOW)
    assert v.intervention == "restrict" and v.reason_code == "override_restrict"
    assert v.override["operator"] == "dispatcher-1"
    assert v.explain_refs == ["ov-1"]


def test_override_clear_allows(engine, predicates) -> None:
    p = predicates["allow_dispatch"]
    v = engine.evaluate(p, FakeDecision(r_milli=900, explain=["ev-1"]), FakeOverride(level="clear"),
                        HysteresisState("none", None, None), NOW)
    assert v.intervention == "none" and v.allow is True and v.reason_code == "override_clear"


def test_override_expiry_restores_function_value(engine, predicates) -> None:
    """属性：override 到期后输出恢复纯函数值（投影器负责到期过滤，此处验证引擎行为）。"""
    p = predicates["allow_dispatch"]
    expired = FakeOverride(level="clear", expires_at=NOW - timedelta(hours=1))
    # 投影器对已到期 override 返回 None（见 projector._derive_override），引擎回到 r 求值
    v = engine.evaluate(p, FakeDecision(r_milli=900, explain=["ev-1"]), None,
                        HysteresisState("none", None, None), NOW)
    assert v.intervention == "lock" and v.reason_code == "entered"
    assert expired.expires_at < NOW  # 到期语义由投影器保证


# ------------------------------------------------------------- service wiring


def test_service_state_persistence_across_calls(service) -> None:
    service.projector._decision = FakeDecision(r_milli=900, explain=["ev-1"])
    v1 = service.evaluate("obj-1", "allow_dispatch", NOW)
    assert v1.intervention == "lock"
    service.projector._decision = FakeDecision(r_milli=780, explain=["ev-1"], as_of=NOW + timedelta(minutes=1))
    v2 = service.evaluate("obj-1", "allow_dispatch", NOW + timedelta(minutes=1))
    assert v2.intervention == "lock" and v2.reason_code == "hysteresis_hold"  # 状态跨调用保持
    service.projector._decision = FakeDecision(r_milli=700, explain=["ev-1"], as_of=NOW + timedelta(minutes=2))
    v3 = service.evaluate("obj-1", "allow_dispatch", NOW + timedelta(minutes=2))
    assert v3.intervention == "none" and v3.reason_code == "released"


def test_service_api_router(service) -> None:
    from fastapi.testclient import TestClient

    router = service.build_router()
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    service.projector._decision = FakeDecision(r_milli=900, explain=["ev-1"])
    resp = client.post("/evaluate", json={"object_id": "obj-1", "predicate": "allow_dispatch"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intervention"] == "lock" and body["enforced"] is False
    assert body["allow"] is True  # 旁路观察期
