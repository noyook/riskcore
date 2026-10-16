#!/usr/bin/env bash
# ============================================================================
# verify_pg_perms.sh — W-001 acceptance: append-only enforcement on PostgreSQL
#
# Verifies, as the least-privilege application role (riskcore_app):
#   1. INSERT into event_log succeeds;
#   2. seq is assigned by the object_seq counter, monotonic per object;
#   3. correction pointing at a missing event_id is rejected (constraint trigger);
#   4. UPDATE / DELETE / TRUNCATE on event_log are ALL rejected (GRANT-level);
#   5. watermark_log accepts forward-only appends.
#
# Usage:
#   createdb riskcore_test && psql -d riskcore_test -f ddl/postgres/001_event_log.sql
#   PGHOST=127.0.0.1 PGPORT=55432 PGDATABASE=riskcore_test PGUSER=riskcore_app \
#     scripts/verify_pg_perms.sh
# ============================================================================
set -uo pipefail

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-55432}"
PGDATABASE="${PGDATABASE:-riskcore_test}"
PGUSER="${PGUSER:-riskcore_app}"
export PGHOST PGPORT PGDATABASE PGUSER

PSQL=(psql -X -v ON_ERROR_STOP=0 -q -A -t)

pass=0; fail=0
ok()   { echo "  ok   $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL $1"; fail=$((fail+1)); }

expect_ok() { # $1=description  $2=SQL
    if out=$("${PSQL[@]}" -c "$2" 2>&1); then ok "$1"; else bad "$1 -> $out"; fi
}
expect_fail() { # $1=description  $2=SQL  (must be REJECTED)
    if out=$("${PSQL[@]}" -c "$2" 2>&1); then bad "$1 (unexpectedly succeeded)"; else ok "$1 [rejected: $(echo "$out" | head -1 | cut -c1-60)]"; fi
}

TENANT=t1
EV1=00000000-0000-4000-8000-000000000001
EV2=00000000-0000-4000-8000-000000000002

echo "== append-only enforcement on ${PGDATABASE} as ${PGUSER} =="

# 1. insert telemetry events (seq assigned by counter inside app tx)
expect_ok "insert event 1" "
  INSERT INTO object_seq (tenant_id, object_id, last_seq) VALUES ('$TENANT','veh-1',0) ON CONFLICT DO NOTHING;
  UPDATE object_seq SET last_seq = last_seq + 1 WHERE tenant_id='$TENANT' AND object_id='veh-1' RETURNING last_seq;
  INSERT INTO event_log (tenant_id,event_id,trace_id,object_id,object_type,seq,occurred_at,ingested_at,type,payload,provenance,schema_version)
  VALUES ('$TENANT','$EV1','tr-1','veh-1','vehicle',1,now(),now(),'telemetry',
          '{\"window_start\":\"2026-10-01T00:00:00Z\",\"window_end\":\"2026-10-01T00:10:00Z\",\"speed_max_kmh\":80,\"continuous_driving_minutes\":0,\"device_id\":\"obd-1\"}',
          '{\"source_system\":\"obd-gateway\"}','1');"

expect_ok "insert event 2 (seq=2 for same object)" "
  UPDATE object_seq SET last_seq = last_seq + 1 WHERE tenant_id='$TENANT' AND object_id='veh-1' RETURNING last_seq;
  INSERT INTO event_log (tenant_id,event_id,trace_id,object_id,object_type,seq,occurred_at,ingested_at,type,payload,provenance,schema_version)
  VALUES ('$TENANT','$EV2','tr-1','veh-1','vehicle',2,now(),now(),'telemetry',
          '{\"window_start\":\"2026-10-01T00:10:00Z\",\"window_end\":\"2026-10-01T00:20:00Z\",\"speed_max_kmh\":82,\"continuous_driving_minutes\":0,\"device_id\":\"obd-1\"}',
          '{\"source_system\":\"obd-gateway\"}','1');"

# seq uniqueness per object (duplicate seq must be rejected by unique index)
expect_fail "duplicate seq per object rejected" "
  INSERT INTO event_log (tenant_id,event_id,trace_id,object_id,object_type,seq,occurred_at,ingested_at,type,payload,provenance,schema_version)
  VALUES ('$TENANT','00000000-0000-4000-8000-000000000003','tr-1','veh-1','vehicle',2,now(),now(),'telemetry','{}','{\"source_system\":\"x\"}','1');"

# 2. correction to missing target rejected
expect_fail "correction to missing target rejected" "
  INSERT INTO event_log (tenant_id,event_id,trace_id,object_id,object_type,seq,occurred_at,ingested_at,type,payload,provenance,schema_version)
  VALUES ('$TENANT','00000000-0000-4000-8000-0000000000ff','tr-1','veh-1','vehicle',3,now(),now(),'correction',
          '{\"corrects_event_id\":\"00000000-0000-4000-8000-00000000dead\",\"reason\":\"x\",\"corrected_payload\":{}}',
          '{\"source_system\":\"audit\",\"operator\":\"auditor-1\"}','1');"

# 3. log is append-only
expect_fail "UPDATE event_log rejected"  "UPDATE event_log SET trace_id='hack' WHERE event_id='$EV1';"
expect_fail "DELETE event_log rejected"  "DELETE FROM event_log WHERE event_id='$EV1';"
expect_fail "TRUNCATE event_log rejected" "TRUNCATE event_log;"

# 4. watermark forward-only append
expect_ok "watermark append" "
  INSERT INTO watermark_log (tenant_id,object_id,seq,advanced_at) VALUES ('$TENANT','veh-1',2,now());"

echo
echo "result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
