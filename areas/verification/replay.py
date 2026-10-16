"""Golden replay runner — the bit-exact reproducibility gate (test-plan.md §1).

Fixtures pin (object event stream, as_of, policy_id). The runner rebuilds the
log from the fixture, projects it, and compares the canonical RiskOutput JSON
against the recorded expectation. Any difference — including key order — is a
failure. Re-record ONLY via --record after human review (参数/函数变更必须重跑
全量，见 test-plan.md).

Usage:
    python3 -m verification.replay            # verify against goldens
    python3 -m verification.replay --record   # regenerate expectations (review!)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "areas" / "event-log"))
sys.path.insert(0, str(ROOT / "areas" / "risk-function"))

from eventlog import EventEnvelope, SqliteStore  # noqa: E402
from eventlog.projector import SqliteProjector  # noqa: E402
from riskfn import load_policy, risk  # noqa: E402
from riskfn import RiskContext  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
FIXTURES = GOLDEN_DIR / "fixtures.json"


def _load_fixtures() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _run_fixture(fixture: dict, tmp_dir: Path) -> str:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(tmp_dir / "replay.db")
    try:
        for raw in fixture["events"]:
            store.append(EventEnvelope.model_validate(raw))
        projector = SqliteProjector(store, load_policy(fixture["policy_id"]), "t1")
        out = projector.project_object(fixture["object_id"], datetime.fromisoformat(fixture["as_of"]))
        return out.decision.canonical_json()
    finally:
        store.close()


def verify(verbose: bool = True) -> int:
    fixtures = _load_fixtures()
    failures = 0
    for fixture in fixtures:
        expected_path = GOLDEN_DIR / f"{fixture['name']}.expected.json"
        if not expected_path.exists():
            print(f"FAIL {fixture['name']}: missing golden file {expected_path.name}")
            failures += 1
            continue
        expected = expected_path.read_text(encoding="utf-8").strip()
        actual = _run_fixture(fixture, ROOT / ".tmp" / "replay")
        if actual == expected:
            if verbose:
                print(f"ok   {fixture['name']}: bit-identical replay")
        else:
            failures += 1
            print(f"FAIL {fixture['name']}: replay diverged")
            print(f"  expected: {expected[:200]}")
            print(f"  actual:   {actual[:200]}")
    print(f"\ngolden replay: {len(fixtures) - failures}/{len(fixtures)} bit-identical")
    return 1 if failures else 0


def record() -> int:
    fixtures = _load_fixtures()
    for fixture in fixtures:
        actual = _run_fixture(fixture, ROOT / ".tmp" / "replay")
        (GOLDEN_DIR / f"{fixture['name']}.expected.json").write_text(actual + "\n", encoding="utf-8")
        print(f"recorded {fixture['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(record() if "--record" in sys.argv else verify())
