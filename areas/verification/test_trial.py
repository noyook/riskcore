"""W-007 trial evaluation tests — 评分判定 / 记录留痕 / 台账重建。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from eventlog import EventType, SqliteStore
from eventlog.app import create_app
from eventlog.trial_api import TrialSubmission, build_trial_router, evaluate_submission

AS_OF = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)


def submission(**kw) -> TrialSubmission:
    base = dict(
        evaluator="张三",
        role="dispatcher",
        object_id="veh-001",
        elapsed_seconds=18,
        scores={"conclusion": 5, "drilldown": 4, "confidence": 4, "intervention": 5, "overall": 5},
        would_rely="yes",
        comments="解除条件很清楚",
        started_at=AS_OF - timedelta(seconds=18),
    )
    base.update(kw)
    return TrialSubmission(**base)


# ------------------------------------------------------------------- 判定逻辑


def test_pass_rule_30s_and_avg_4() -> None:
    v = evaluate_submission(submission())
    assert v["within_30s"] is True and v["passed"] is True
    assert v["average_score_milli"] == 4600


def test_fail_when_over_30s() -> None:
    v = evaluate_submission(submission(elapsed_seconds=45))
    assert v["within_30s"] is False and v["passed"] is False


def test_fail_when_avg_below_4() -> None:
    v = evaluate_submission(submission(
        scores={"conclusion": 3, "drilldown": 3, "confidence": 3, "intervention": 3, "overall": 3}
    ))
    assert v["within_30s"] is True and v["average_score_milli"] == 3000 and v["passed"] is False


def test_override_optional_and_range_checked() -> None:
    v = evaluate_submission(submission(scores={"conclusion": 4, "drilldown": 4, "confidence": 4,
                                               "intervention": 4, "override": 2, "overall": 4}))
    assert v["recorded_scores"]["override"] == 2
    with pytest.raises(Exception):
        evaluate_submission(submission(scores={"conclusion": 9, "drilldown": 4, "confidence": 4,
                                               "intervention": 4, "overall": 4}))


def test_missing_required_score_rejected() -> None:
    with pytest.raises(Exception, match="missing scores"):
        evaluate_submission(submission(scores={"conclusion": 5, "overall": 5}))


# --------------------------------------------------------------------- API 端到端


@pytest.fixture
def client(tmp_path):
    store = SqliteStore(tmp_path / "trial.db")
    app = create_app(store)
    app.include_router(build_trial_router())
    yield TestClient(app), store
    store.close()


def test_submit_and_records_roundtrip(client) -> None:
    c, store = client
    resp = c.post("/trial/submit", json=submission().model_dump(mode="json"),
                  headers={"X-Tenant-Id": "t1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True and body["seq"] == 1

    # 记录以 projection_only 入日志（不建表）
    events = store.read_object_events("t1", "veh-001")
    trial = [e for e in events if e.type is EventType.PROJECTION_ONLY
             and e.payload.get("material_kind") == "trial_record"]
    assert len(trial) == 1
    assert trial[0].payload["data"]["evaluator"] == "张三"

    # 台账从日志重建
    rec = c.get("/trial/records", headers={"X-Tenant-Id": "t1"}).json()
    assert rec["total"] == 1 and rec["passed"] == 1 and rec["pass_rate_milli"] == 1000


def test_trial_requires_tenant(client) -> None:
    c, _ = client
    assert c.post("/trial/submit", json=submission().model_dump(mode="json")).status_code == 401
    assert c.get("/trial/records").status_code == 401


def test_multiple_trials_pass_rate(client) -> None:
    c, _ = client
    c.post("/trial/submit", json=submission().model_dump(mode="json"), headers={"X-Tenant-Id": "t1"})
    c.post("/trial/submit", json=submission(evaluator="李四", elapsed_seconds=52,
                                            scores={"conclusion": 3, "drilldown": 2, "confidence": 3,
                                                    "intervention": 3, "overall": 3}).model_dump(mode="json"),
           headers={"X-Tenant-Id": "t1"})
    rec = c.get("/trial/records", headers={"X-Tenant-Id": "t1"}).json()
    assert rec["total"] == 2 and rec["passed"] == 1 and rec["pass_rate_milli"] == 500
