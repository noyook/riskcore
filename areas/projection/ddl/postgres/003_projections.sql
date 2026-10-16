-- ============================================================================
-- 003_projections.sql — regulatory audit views (PostgreSQL 15)
--
-- Python generators in areas/projection/projection/ are the authoritative
-- artifact producers; these views exist so an auditor can run the same
-- queries by hand against the caches. Projection area rule: read-only,
-- regenerable, never the source of truth.
-- ============================================================================

BEGIN;

-- 四色风险分布（阈值语义见 projections.md；数值取自 policy.executor）
CREATE OR REPLACE VIEW v_four_color AS
SELECT
    d.object_id,
    s.object_type,
    d.policy_id,
    d.r_milli,
    d.veto_active,
    CASE
        WHEN d.veto_active OR d.r_milli >= 800 THEN 'red'
        WHEN d.r_milli >= 600 THEN 'orange'
        WHEN d.r_milli >= 400 THEN 'yellow'
        ELSE 'blue'
    END AS color,
    d.as_of
FROM decision_cache d
JOIN projection_state s ON s.tenant_id = d.tenant_id AND s.object_id = d.object_id;

-- 双重预防闭环台账
CREATE OR REPLACE VIEW v_dual_prevention_ledger AS
WITH created AS (
    SELECT object_id,
           payload->>'hazard_ref' AS hazard_ref,
           payload->>'order_type'  AS order_type,
           payload->>'due_date'    AS due_date,
           event_id, occurred_at
    FROM event_log WHERE type = 'work_order_created'
),
closed AS (
    SELECT payload->>'closes_order_id' AS hazard_ref,
           event_id AS closed_event, occurred_at AS closed_at,
           payload->>'acceptor' AS acceptor
    FROM event_log WHERE type = 'work_order_closed'
)
SELECT c.*, cl.closed_event, cl.closed_at, cl.acceptor,
       CASE WHEN cl.closed_event IS NULL AND c.due_date < current_date THEN 'overdue'
            WHEN cl.closed_event IS NULL THEN 'open'
            ELSE 'closed' END AS status
FROM created c
LEFT JOIN closed cl ON cl.hazard_ref = c.hazard_ref;

GRANT SELECT ON v_four_color, v_dual_prevention_ledger TO riskcore_app;

COMMIT;
