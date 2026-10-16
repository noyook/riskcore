"""Policy document loader + validator (policy-format.md).

Guarantees enforced at load time (fail fast, AGENTS.md Phase-2 discipline):
- policy_id + effective_from present; weights are integers in [0, 1000];
- every weight references a known fact (facts.py dictionary) — no bare numbers;
- every weight and every whitelist lambda carries a provenance entry with
  source + calibration_support (W-003 acceptance: 无裸数字);
- veto_types are known veto names; thresholds are strictly ordered;
- half-lives are positive; explain_top_k >= 1.

Old versions are never mutated: v2 is a new file, history replays with the
policy_id pinned at computation time (D-007).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .facts import FACT_NAMES, KNOWN_VETO_NAMES

POLICIES_DIR = Path(__file__).resolve().parent.parent / "policies"


class FactorRatio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ratio"]
    cap: int = Field(ge=1)


class FactorExpiry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["expiry"]
    warn_days: int = Field(ge=1)


class InteractionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pair: tuple[str, str]
    lambda_milli: int = Field(ge=0, le=1000)
    calibration_support: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> "InteractionRule":
        if self.pair[0] == self.pair[1]:
            raise ValueError(f"interaction pair must reference two distinct facts: {self.pair}")
        return self


class ProvenanceEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: Literal["historical_data", "expert_prior", "legacy_redline"]
    calibration_support: int = Field(ge=0)
    note: str = ""


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")
    veto_types: list[str]
    sensor_thresholds: dict[str, int]
    weights_milli: dict[str, int]
    decay: dict[str, dict[str, int]]
    factor_params: dict[str, FactorRatio | FactorExpiry]
    interaction_whitelist: list[InteractionRule]
    prior_injection: dict[str, int]
    missing_factor: str
    explain_top_k: int = Field(ge=1)
    provenance: dict[str, object]


class ExecutorCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thresholds: dict[str, int]
    hysteresis_milli: int = Field(ge=0, le=200)
    manual_override_ttl_hours: int = Field(ge=1)


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(pattern=r"^v[0-9]+(\.[0-9]+)?$")
    effective_from: str  # ISO date
    scoring: Scoring
    executor: ExecutorCfg
    clause_map: dict[str, str]

    @model_validator(mode="after")
    def _validate(self) -> "PolicyDocument":
        s = self.scoring
        known = set(FACT_NAMES)
        for name, w in s.weights_milli.items():
            if name not in known:
                raise ValueError(f"weight references unknown fact '{name}' (see riskfn/facts.py)")
            if not 0 <= w <= 1000:
                raise ValueError(f"weight '{name}'={w} outside [0,1000] milli")
            if name not in s.factor_params:
                raise ValueError(f"fact '{name}' has a weight but no factor_params mapping")

        # provenance completeness: every weight + every lambda must be sourced
        weight_prov = s.provenance.get("weights_milli")
        if not isinstance(weight_prov, dict):
            raise ValueError("scoring.provenance.weights_milli missing")
        missing = set(s.weights_milli) - set(weight_prov)
        if missing:
            raise ValueError(f"weights without provenance (无裸数字): {sorted(missing)}")
        for name, entry in weight_prov.items():
            ProvenanceEntry.model_validate(entry)

        for rule in s.interaction_whitelist:
            for f in rule.pair:
                if f not in known:
                    raise ValueError(f"interaction pair references unknown fact '{f}'")
            key = "|".join(rule.pair)
            lp = s.provenance.get("interaction_whitelist")
            if not isinstance(lp, dict) or key not in lp:
                raise ValueError(f"interaction lambda without provenance: {key}")

        for veto in s.veto_types:
            if veto not in KNOWN_VETO_NAMES:
                raise ValueError(f"unknown veto type '{veto}'; known: {KNOWN_VETO_NAMES}")

        for carrier, hl in s.decay.get("half_life_days", {}).items():
            if hl <= 0:
                raise ValueError(f"half-life for '{carrier}' must be positive")

        th = self.executor.thresholds
        if not (th["alert"] < th["restrict"] < th["lock"]):
            raise ValueError(f"thresholds must be ordered alert<restrict<lock: {th}")

        for clause_key in (*s.veto_types, "alert", "restrict", "lock"):
            if clause_key not in self.clause_map:
                raise ValueError(f"clause_map missing entry for '{clause_key}'")

        return self


def load_policy(policy_id: str, policies_dir: Path | None = None) -> PolicyDocument:
    base = policies_dir or POLICIES_DIR
    path = base / f"policy-{policy_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"policy '{policy_id}' not found at {path}")
    return PolicyDocument.model_validate(json.loads(path.read_text(encoding="utf-8")))


def list_policy_ids(policies_dir: Path | None = None) -> list[str]:
    base = policies_dir or POLICIES_DIR
    return sorted(p.stem.removeprefix("policy-") for p in base.glob("policy-*.json"))
