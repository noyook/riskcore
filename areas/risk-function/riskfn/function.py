"""risk(snapshot, policy, ctx) — the deterministic pure function (W-004 v0).

Four iron laws made executable (AGENTS.md 一):
  1. 时钟外置 — the only time input is ctx.as_of / snapshot.as_of;
  2. 序不乱   — only snapshot facts (already seq <= watermark) are read;
  3. 无副作用 — no IO, no DB, no network, no randomness, no floats;
  4. 政策版本化 — every number comes from `policy`; policy_id travels in ctx.

Computation order (function-contract.md, strictly top-down):
  1. veto override      — snapshot.veto_flags hit → r=1000, skip everything;
  2. prior injection    — sensor facts missing/stale → LEC-D baseline once;
  3. single factors     — Σ w_i · g_i(decayed fact), integer milli;
  4. interactions       — v0: interface only (whitelist validated, not
                          evaluated; evaluation ships in function v1.1);
  5. saturation         — clamp to [0, 1000];
  6. confidence/explain — n_events, window coverage, data freshness, top-k
                          event refs ordered by contribution.

Determinism: fact iteration is over sorted keys; per-event decay truncates
identically on every run; ties in explain break by event_id. Two runs on the
same (snapshot, policy, ctx) produce byte-identical canonical JSON — the
golden replay gate in areas/verification/.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .decay import age_quanta_milli, decay_milli
from .facts import COUNT_FACTS, EXPIRY_FACTS, FactSpec
from .policy import PolicyDocument

FUNCTION_VERSION = "0.1.0"

#: Facts whose absence is a SENSOR gap → prior injection applies (D-008, T2).
SENSOR_FACTS = frozenset({"telemetry_fatigue_windows", "telemetry_speeding_windows"})

_FACT_SPECS: dict[str, FactSpec] = {f.name: f for f in (*COUNT_FACTS, *EXPIRY_FACTS)}


class RiskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str
    as_of: datetime
    policy_id: str
    watermark: int = 0


class Confidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_events: int
    window_coverage_milli: int
    data_freshness_milli: int
    calibration_notes: list[str] = Field(default_factory=list)


class ExplainRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    fact: str
    contribution_milli: int
    kind: str  # veto | factor | prior


class RiskOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    r_milli: int = Field(ge=0, le=1000)
    veto_active: bool
    veto_names: list[str] = Field(default_factory=list)
    prior_injected: bool = False
    confidence: Confidence
    explain: list[ExplainRef] = Field(default_factory=list)
    policy_id: str
    function_version: str = FUNCTION_VERSION
    as_of: datetime
    object_id: str
    watermark_seq: int = 0

    def canonical_json(self) -> str:
        return self.model_dump_json(indent=None)


def _half_life_for(policy: PolicyDocument, carrier: str) -> int:
    hl = policy.scoring.decay["half_life_days"]
    if carrier not in hl:
        raise ValueError(f"policy has no half-life for carrier '{carrier}'")
    return hl[carrier]


def _veto_output(snapshot, ctx: RiskContext, policy: PolicyDocument) -> RiskOutput:
    veto_names = sorted(snapshot.veto_flags)
    veto_facts = sorted(
        name for name, spec in _FACT_SPECS.items() if spec.veto_carrier in set(veto_names)
    )
    veto_refs: list[ExplainRef] = []
    seen: set[str] = set()
    for name in veto_facts:
        entry = snapshot.facts.get(name)
        if entry is None:
            # Projector contract violation: a veto flag must be backed by its
            # triggering fact. Fail loud rather than emit an unexplainable 1000.
            raise ValueError(
                f"veto flag '{_FACT_SPECS[name].veto_carrier}' has no backing fact "
                f"'{name}' in snapshot — projector contract violation"
            )
        if not entry.contributions:
            raise ValueError(
                f"veto fact '{name}' carries no triggering event contributions — "
                "projector contract violation"
            )
        for c in sorted(entry.contributions, key=lambda c: c.event_id):
            if c.event_id not in seen:
                seen.add(c.event_id)
                veto_refs.append(
                    ExplainRef(event_id=c.event_id, fact=name, contribution_milli=1000, kind="veto")
                )
    return RiskOutput(
        r_milli=1000,
        veto_active=True,
        veto_names=sorted(snapshot.veto_flags),
        confidence=Confidence(
            n_events=len(seen),
            window_coverage_milli=1000 if seen else 0,
            data_freshness_milli=1000,
        ),
        explain=veto_refs,
        policy_id=ctx.policy_id,
        as_of=ctx.as_of,
        object_id=ctx.object_id,
        watermark_seq=ctx.watermark,
    )


def risk(snapshot, policy: PolicyDocument, ctx: RiskContext) -> RiskOutput:
    # ---- 1. 强制条款覆盖（最顶层第一步，优先于一切算分） --------------------
    if snapshot.veto_flags:
        return _veto_output(snapshot, ctx, policy)

    s = policy.scoring
    total = 0
    explain: list[ExplainRef] = []
    contributing_events: set[str] = set()

    # ---- 2. 先验注入（传感器缺位时的诚实兜底，D-008） ----------------------
    sensor_gap = any(
        snapshot.facts.get(name) is None or snapshot.facts[name].freshness != "valid"
        for name in sorted(SENSOR_FACTS)
    )
    prior_injected = False
    if sensor_gap:
        prior_entry = snapshot.facts.get("lecd_prior")
        if (
            prior_entry is not None
            and prior_entry.freshness == "valid"
            and prior_entry.prior_r_milli is not None
        ):
            strength = s.prior_injection["strength_milli"]
            injected = (prior_entry.prior_r_milli * strength) // 1000
            total += injected
            prior_injected = True
            for c in prior_entry.contributions:
                contributing_events.add(c.event_id)
                explain.append(
                    ExplainRef(
                        event_id=c.event_id,
                        fact="lecd_prior",
                        contribution_milli=injected,
                        kind="prior",
                    )
                )

    # ---- 3. 单因子项 -------------------------------------------------------
    for fact_name in sorted(s.weights_milli):
        entry = snapshot.facts.get(fact_name)
        if entry is None or entry.freshness != "valid":
            continue  # missing_factor=ignore_with_coverage_penalty → 0 分 + 覆盖率下降
        params = s.factor_params[fact_name]
        weight = s.weights_milli[fact_name]
        spec = _FACT_SPECS[fact_name]

        if spec.unit == "days_remaining":
            if entry.days_remaining is None:
                continue
            g = _expiry_g(entry.days_remaining, params)
            term = (weight * g) // 1000
            total += term
            for c in entry.contributions:
                contributing_events.add(c.event_id)
                explain.append(
                    ExplainRef(
                        event_id=c.event_id, fact=fact_name, contribution_milli=term, kind="factor"
                    )
                )
            continue

        # count-like facts: per-event decay in MILLI scale (no per-event
        # truncation — a single window with value=1 must survive), then
        # ratio mapping against cap*1000.
        half_life = _half_life_for(policy, spec.carrier)
        decayed_total_milli = 0
        event_milli: list[tuple[str, int]] = []
        for c in entry.contributions:
            m = c.value * decay_milli(age_quanta_milli(c.age_days, half_life))
            decayed_total_milli += m
            contributing_events.add(c.event_id)
            event_milli.append((c.event_id, m))
        if decayed_total_milli <= 0:
            continue
        g = _ratio_g_milli(decayed_total_milli, params)
        term = (weight * g) // 1000
        total += term
        # per-event share for explain (proportional, deterministic)
        for event_id, m in event_milli:
            if m <= 0:
                continue
            event_term = (term * m) // decayed_total_milli
            explain.append(
                ExplainRef(
                    event_id=event_id, fact=fact_name, contribution_milli=event_term, kind="factor"
                )
            )

    # ---- 4. 交互项（v0：仅接口，白名单已由 loader 校验；v1.1 开求值） --------
    # for rule in s.interaction_whitelist: ... (deferred, see function docstring)

    # ---- 5. 饱和 -----------------------------------------------------------
    r_milli = max(0, min(1000, total))

    # ---- 6. 置信与解释 ------------------------------------------------------
    n_events = len(contributing_events)
    tracked = len(snapshot.facts)
    valid_count = sum(1 for e in snapshot.facts.values() if e.freshness == "valid")
    coverage = (valid_count * 1000) // tracked if tracked else 0

    freshness_sum = 0
    freshness_n = 0
    for name in sorted(snapshot.facts):
        entry = snapshot.facts[name]
        if entry.freshness != "valid" or not entry.contributions:
            continue
        spec = _FACT_SPECS.get(name)
        carrier = spec.carrier if spec else "violation"
        half_life = _half_life_for(policy, carrier)
        latest_age = max(c.age_days for c in entry.contributions)
        freshness_sum += decay_milli(age_quanta_milli(latest_age, half_life))
        freshness_n += 1
    freshness = (freshness_sum // freshness_n) if freshness_n else 0

    explain.sort(key=lambda e: (-e.contribution_milli, e.event_id))
    top_k = s.explain_top_k
    if len(explain) > top_k:
        explain = explain[:top_k]

    return RiskOutput(
        r_milli=r_milli,
        veto_active=False,
        veto_names=[],
        prior_injected=prior_injected,
        confidence=Confidence(
            n_events=n_events,
            window_coverage_milli=coverage,
            data_freshness_milli=freshness,
        ),
        explain=explain,
        policy_id=ctx.policy_id,
        as_of=ctx.as_of,
        object_id=ctx.object_id,
        watermark_seq=ctx.watermark,
    )


def _ratio_g_milli(value_milli: int, params):
    """min(value, cap)/cap in milli, value already in milli scale."""
    from .factors import g_ratio_milli

    return g_ratio_milli(value_milli, params)


def _expiry_g(days_remaining: int, params):
    from .factors import g_expiry

    return g_expiry(days_remaining, params)
