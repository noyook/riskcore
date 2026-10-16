#!/usr/bin/env bash
# ============================================================================
# verify_all.sh — the CI gate (verification area rule 1: 任一失败禁止合并)
#
#   1. 黄金重放     — fixed (object, as_of, policy_id) → bit-identical output
#   2. 属性测试     — veto / saturation / monotonicity / hysteresis / correction
#   3. 对账检查     — projector drift = 0
#   4. PG 权限验证  — append-only enforcement (skipped when no PG reachable)
#
# Usage: scripts/verify_all.sh   (exit 0 = green)
# ============================================================================
set -uo pipefail

PY="${PYTHON:-/Users/caocao/.workbuddy/binaries/python/envs/default/bin/python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail=0

echo "== [1/4] 黄金重放 =="
"$PY" -m areas.verification.replay || fail=1

echo
echo "== [2/4] 属性测试 + 全套件 =="
"$PY" -m pytest areas/verification/ -q || fail=1

echo
echo "== [3/4] 对账检查（漂移率必须为 0） =="
"$PY" - <<'EOF' || fail=1
import sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, "areas/event-log"); sys.path.insert(0, "areas/risk-function"); sys.path.insert(0, "areas/verification")
from eventlog import SqliteStore
from eventlog.projector import SqliteProjector
from eventlog.reconciler import Reconciler
from riskfn import load_policy
from factories import AS_OF, TENANT, telemetry_event, violation_event

EARLY = AS_OF - timedelta(days=2)
with tempfile.TemporaryDirectory() as d:
    store = SqliteStore(Path(d) / "log.db")
    policy = load_policy("v1")
    projector = SqliteProjector(store, policy, TENANT)
    for i in range(3):
        store.append(violation_event(f"veh-{i}", EARLY, kind="speeding", count=2 + i))
        store.append(telemetry_event(f"veh-{i}", EARLY, continuous_driving_minutes=300))
    projector.project_all(AS_OF)
    reports = Reconciler(store, projector, TENANT).reconcile_full(AS_OF)
    drift = [r for r in reports if r.drifted]
    print(f"reconciled {len(reports)} objects, drift={len(drift)}")
    raise SystemExit(1 if drift else 0)
EOF

echo
echo "== [4/4] PostgreSQL append-only 权限验证 =="
if [ -n "${PGHOST:-}" ] && command -v psql >/dev/null 2>&1; then
    PGHOST="${PGHOST}" PGPORT="${PGPORT:-55432}" PGDATABASE="${PGDATABASE:-riskcore_test}" \
      PGUSER="${PGUSER:-riskcore_app}" scripts/verify_pg_perms.sh || fail=1
else
    echo "skip: PG 未配置（PGHOST 未设置）——CI 环境应配置并真机验证"
fi

echo
if [ "$fail" -eq 0 ]; then echo "GATE GREEN: 黄金重放 + 属性测试 + 对账 全部通过"; else echo "GATE RED: 存在失败项，禁止合并"; fi
exit "$fail"
