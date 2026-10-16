"""ProjectionService — the four regulatory artifacts + regeneration SOP.

Rules (projections area):
  1. every artifact is stamped (function_version + policy_id + as_of + regenerated_at);
  2. artifacts are pure functions of (log range + policy_id) — regeneration is
     idempotent: same input → identical output except regenerated_at;
  3. a regulatory realignment changes the projection only — never the kernel;
  4. old versions are archived, never overwritten (path carries policy+as_of).

The kernel is never written to; this service only reads.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from riskfn import PolicyDocument

from . import doc6, fourcolor, ledger, lecd
from .stamp import assert_stamped, strip_regenerated_at

OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs" / "projections"


class ProjectionService:
    def __init__(self, store, projector, executor, policy: PolicyDocument, tenant_id: str,
                 artifacts_dir: Path | None = None) -> None:
        self.store = store
        self.projector = projector
        self.executor = executor
        self.policy = policy
        self.tenant = tenant_id
        self.artifacts_dir = artifacts_dir or OUTPUTS_DIR

    # -------------------------------------------------------------- generators

    def generate_fourcolor(self, as_of: datetime, regenerated_at: datetime) -> dict:
        rows = []
        for oid in self.store.iter_objects(self.tenant):
            decision = self.projector.read_decision(oid)
            if decision is None:
                continue
            rows.append(
                {
                    "object_id": oid,
                    "object_type": self.projector.object_type_of(oid) or "",
                    "r_milli": decision.r_milli,
                    "veto_active": decision.veto_active,
                }
            )
        return fourcolor.build_fourcolor(rows, self.policy, as_of, regenerated_at)

    def generate_lecd(self, as_of: datetime, regenerated_at: datetime) -> dict:
        events = [
            e
            for oid in self.store.iter_objects(self.tenant)
            for e in self.store.read_object_events(self.tenant, oid)
            if e.type.value == "expert_assertion"
        ]
        return lecd.build_lecd_report(events, self.policy.policy_id, as_of, regenerated_at)

    def generate_doc6(self, as_of: datetime, regenerated_at: datetime) -> dict:
        interventions = []
        for oid in self.store.iter_objects(self.tenant):
            for iv in self.executor.active_interventions(oid):
                interventions.append({"object_id": oid, **iv})
        return doc6.build_doc6_list(interventions, self.policy.policy_id, as_of, regenerated_at)

    def generate_ledger(self, as_of: datetime, regenerated_at: datetime) -> dict:
        events = [
            e
            for oid in self.store.iter_objects(self.tenant)
            for e in self.store.read_object_events(self.tenant, oid)
        ]
        return ledger.build_ledger(events, self.policy.policy_id, as_of, regenerated_at)

    # ------------------------------------------------------------ regeneration

    def regenerate_all(self, as_of: datetime, regenerated_at: datetime) -> dict[str, Path]:
        """Idempotent regeneration + persistence (old versions kept on disk)."""
        generators = {
            "four_color": self.generate_fourcolor,
            "lecd_annual": self.generate_lecd,
            "doc6_list": self.generate_doc6,
            "dual_prevention_ledger": self.generate_ledger,
        }
        paths: dict[str, Path] = {}
        for kind, gen in generators.items():
            artifact = gen(as_of, regenerated_at)
            assert_stamped(artifact)
            out_dir = self.artifacts_dir / kind
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{self.policy.policy_id}-{as_of.date().isoformat()}.json"
            path.write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            paths[kind] = path
        return paths

    def build_router(self):
        from fastapi import APIRouter, Query

        service = self
        router = APIRouter(tags=["projection"])

        @router.get("/projections/{kind}")
        def get_projection(kind: str, as_of: str | None = Query(None)) -> dict:
            from datetime import datetime, timezone

            parsed = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
            now = datetime.now(timezone.utc)
            gen = {
                "four_color": service.generate_fourcolor,
                "lecd_annual": service.generate_lecd,
                "doc6_list": service.generate_doc6,
                "dual_prevention_ledger": service.generate_ledger,
            }.get(kind)
            if gen is None:
                return {"error": f"unknown projection '{kind}'"}
            return gen(parsed, now)

        @router.post("/projections/regenerate")
        def regenerate(as_of: str | None = Query(None)) -> dict:
            from datetime import datetime, timezone

            parsed = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
            paths = service.regenerate_all(parsed, datetime.now(timezone.utc))
            return {"regenerated": {k: str(v) for k, v in paths.items()}}

        return router


__all__ = ["ProjectionService", "assert_stamped", "strip_regenerated_at"]
