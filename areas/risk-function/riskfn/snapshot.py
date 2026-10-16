"""StateSnapshot — the function's only input (besides policy + ctx).

Produced by the projector (W-005); read-only for the function. Every tracked
fact appears as an entry — including missing ones — so that confidence's
window coverage is computable without guessing the fact universe.

Freshness tri-state (function-contract.md):
    valid  — fact observed inside its window;
    stale  — observed but outside the freshness window;
    missing— no data at all.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Freshness = Literal["valid", "stale", "missing"]


class FactContribution(BaseModel):
    """One event's raw magnitude and its age at as_of (whole days, floored)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    value: int = Field(ge=0)
    age_days: int = Field(ge=0)


class FactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    freshness: Freshness = "missing"
    contributions: list[FactContribution] = Field(default_factory=list)
    # expiry facts (registry): days remaining at as_of; negative = overdue
    days_remaining: int | None = None
    registry_status: Literal["valid", "expired", "revoked"] | None = None
    # prior fact (expert_assertion): LEC-D D = L*E*C in [1, 1000]
    prior_r_milli: int | None = None


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: str
    as_of: datetime
    watermark_seq: int = 0
    # sorted keys at construction time → deterministic iteration everywhere
    facts: dict[str, FactEntry] = Field(default_factory=dict)
    # projector-derived veto flags, sorted
    veto_flags: list[str] = Field(default_factory=list)

    @staticmethod
    def build(
        object_id: str,
        as_of: datetime,
        facts: dict[str, FactEntry],
        veto_flags: list[str] | None = None,
        watermark_seq: int = 0,
    ) -> "StateSnapshot":
        return StateSnapshot(
            object_id=object_id,
            as_of=as_of,
            watermark_seq=watermark_seq,
            facts=dict(sorted(facts.items())),
            veto_flags=sorted(veto_flags or []),
        )
