"""Version stamp — every regulatory artifact carries the trio (projections.md).

    function_version=x.y | policy_id=vN | as_of=<水位线> | regenerated_at=<时间>

Audit answer on site: "replayed the log with function x.y, policy vN, up to
yesterday's watermark — identical to this report", and it can be replayed on
the spot.
"""
from __future__ import annotations

from datetime import datetime

from riskfn import FUNCTION_VERSION


def build_stamp(policy_id: str, as_of: datetime, regenerated_at: datetime) -> dict:
    return {
        "function_version": FUNCTION_VERSION,
        "policy_id": policy_id,
        "as_of": as_of.isoformat(),
        "regenerated_at": regenerated_at.isoformat(),
    }


def stamp_line(stamp: dict) -> str:
    return (
        f"function_version={stamp['function_version']} | "
        f"policy_id={stamp['policy_id']} | "
        f"as_of={stamp['as_of']} | "
        f"regenerated_at={stamp['regenerated_at']}"
    )


def assert_stamped(artifact: dict) -> None:
    stamp = artifact.get("stamp")
    if not isinstance(stamp, dict):
        raise ValueError("artifact missing version stamp (projections area rule 1)")
    for key in ("function_version", "policy_id", "as_of", "regenerated_at"):
        if not stamp.get(key):
            raise ValueError(f"artifact stamp incomplete: missing {key}")


def strip_regenerated_at(artifact: dict) -> dict:
    """Canonical form for idempotency checks (everything except the stamp's
    regenerated_at must be byte-identical across regenerations)."""
    import copy

    clone = copy.deepcopy(artifact)
    clone.get("stamp", {}).pop("regenerated_at", None)
    return clone
