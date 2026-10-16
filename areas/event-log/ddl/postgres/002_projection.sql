-- ============================================================================
-- 002_projection.sql — projection tables (PostgreSQL 15)
--
-- These are CACHE tables (D-001: 快照只是投影缓存，可随时从日志重建).
-- The projector is the ONLY writer; other areas read through interfaces.
-- Rebuild SOP: TRUNCATE these tables, replay the log (projector.rebuild_all).
-- ============================================================================

BEGIN;

-- fact 快照表（给纯函数的输入；每实体 × 每 fact 一行）
CREATE TABLE IF NOT EXISTS fact_snapshot (
    tenant_id      text        NOT NULL,
    object_id      text        NOT NULL,
    fact_name      text        NOT NULL,
    freshness      text        NOT NULL CHECK (freshness IN ('valid','stale','missing')),
    contributions  jsonb       NOT NULL DEFAULT '[]',
    days_remaining integer,
    registry_status text       CHECK (registry_status IN ('valid','expired','revoked')),
    prior_r_milli integer     CHECK (prior_r_milli IS NULL OR (prior_r_milli BETWEEN 1 AND 1000)),
    last_event_seq bigint      NOT NULL,
    updated_as_of  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, object_id, fact_name)
);

-- 决策状态缓存（给执行器的输入；纯函数输出物化，带 TTL 语义由 stale_after 执行）
CREATE TABLE IF NOT EXISTS decision_cache (
    tenant_id    text        NOT NULL,
    object_id    text        NOT NULL,
    policy_id    text        NOT NULL,
    r_milli      integer     NOT NULL CHECK (r_milli BETWEEN 0 AND 1000),
    veto_active  boolean     NOT NULL,
    veto_names   jsonb       NOT NULL DEFAULT '[]',
    confidence   jsonb       NOT NULL,
    explain_refs jsonb       NOT NULL,
    as_of        timestamptz NOT NULL,
    seq_upto     bigint      NOT NULL,
    computed_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, object_id, policy_id)
);

-- 投影进度（增量处理的游标 + 水位线）
CREATE TABLE IF NOT EXISTS projection_state (
    tenant_id   text    NOT NULL,
    object_id   text    NOT NULL,
    object_type text    NOT NULL,
    last_seq    bigint  NOT NULL DEFAULT 0,
    watermark   bigint  NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, object_id)
);

-- 人工覆写状态（投影器从 manual_override 事件派生；执行器的最高优先级输入，
-- 见 areas/executor/predicates.md §人工覆写的执行语义）
CREATE TABLE IF NOT EXISTS override_state (
    tenant_id  text        NOT NULL,
    object_id  text        NOT NULL,
    level      text        NOT NULL CHECK (level IN ('alert','restrict','lock','clear')),
    operator   text        NOT NULL,
    approver   text        NOT NULL,
    reason     text        NOT NULL,
    expires_at timestamptz NOT NULL,
    event_id   uuid        NOT NULL,
    as_of      timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, object_id)
);

-- 投影器角色：可写缓存表，不可写日志本体
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'riskcore_projector') THEN
        CREATE ROLE riskcore_projector LOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO riskcore_projector;
GRANT SELECT, INSERT, UPDATE, DELETE ON fact_snapshot, decision_cache, projection_state TO riskcore_projector;
GRANT SELECT, INSERT, UPDATE, DELETE ON override_state TO riskcore_projector;
GRANT SELECT, INSERT ON watermark_log TO riskcore_projector;
GRANT SELECT, INSERT ON event_log TO riskcore_projector;   -- 读日志 + 写 projection_only 留痕
REVOKE UPDATE, DELETE ON event_log FROM riskcore_projector;

COMMIT;
