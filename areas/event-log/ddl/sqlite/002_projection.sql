-- ============================================================================
-- 002_projection.sql — projection tables (SQLite dev store)
-- Caches only (D-001): rebuild SOP = DELETE rows, replay the log.
-- ============================================================================

CREATE TABLE IF NOT EXISTS fact_snapshot (
    tenant_id      TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    fact_name      TEXT NOT NULL,
    freshness      TEXT NOT NULL CHECK (freshness IN ('valid','stale','missing')),
    contributions  TEXT NOT NULL DEFAULT '[]',
    days_remaining INTEGER,
    registry_status TEXT CHECK (registry_status IN ('valid','expired','revoked')),
    prior_r_milli INTEGER CHECK (prior_r_milli IS NULL OR (prior_r_milli BETWEEN 1 AND 1000)),
    last_event_seq INTEGER NOT NULL,
    updated_as_of  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, object_id, fact_name)
);

CREATE TABLE IF NOT EXISTS decision_cache (
    tenant_id    TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    policy_id    TEXT NOT NULL,
    r_milli      INTEGER NOT NULL CHECK (r_milli BETWEEN 0 AND 1000),
    veto_active  INTEGER NOT NULL CHECK (veto_active IN (0,1)),
    veto_names   TEXT NOT NULL DEFAULT '[]',
    confidence   TEXT NOT NULL,
    explain_refs TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    seq_upto     INTEGER NOT NULL,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, object_id, policy_id)
);

CREATE TABLE IF NOT EXISTS projection_state (
    tenant_id   TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    object_type TEXT NOT NULL,
    last_seq    INTEGER NOT NULL DEFAULT 0,
    watermark   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, object_id)
);

-- 人工覆写状态（投影器从 manual_override 事件派生；执行器最高优先级输入）
CREATE TABLE IF NOT EXISTS override_state (
    tenant_id  TEXT NOT NULL,
    object_id  TEXT NOT NULL,
    level      TEXT NOT NULL CHECK (level IN ('alert','restrict','lock','clear')),
    operator   TEXT NOT NULL,
    approver   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, object_id)
);
