-- ============================================================================
-- 001_event_log.sql — fact log DDL (PostgreSQL 15, production target)
--
-- 设计依据（areas/event-log/event-schema.md + 区域规则）：
--   R2 日志表只允许 INSERT：应用角色 riskcore_app 只被授予 SELECT/INSERT；
--      UPDATE/DELETE/TRUNCATE 显式 REVOKE（无授权即拒绝，双保险）。
--   R3 seq 对象内单调：由 object_seq 计数器表在写事务内分配，调用方禁传。
--      注意：object_seq 是序列元数据，不是日志本体，允许 UPDATE。
--   R4 correction 完整性：constraint trigger 校验 corrects_event_id 存在
--      （同租户）；DEFERRABLE 使同事务内"先更正后到达"的竞态可排序解决。
--   分区策略：PARTITION BY LIST (tenant_id)。对象级分区会导致分区数随
--      对象数爆炸（万个司机/车辆），无收益；对象内 seq 唯一性由唯一索引
--      (tenant_id, object_id, seq) 保证。W-001 Q1 结论：Postgres 分区表。
--   水位线：watermark_log 只记前进，不回退（迟到事件不回溯已出具决策）。
--   outbox：业务事务内与事件同库写入，relay 异步投递，消费端按 event_id 幂等。
-- ============================================================================

BEGIN;

-- --------------------------------------------------------------------------
-- 1. 事实日志本体（append-only）
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_log (
    tenant_id      text        NOT NULL,
    event_id       uuid        NOT NULL,
    trace_id       text        NOT NULL,
    object_id      text        NOT NULL,
    object_type    text        NOT NULL CHECK (object_type IN (
                        'driver','vehicle','route','cargo','transport_task','enterprise')),
    seq            bigint      NOT NULL CHECK (seq > 0),
    occurred_at    timestamptz NOT NULL,   -- 业务发生时间（区分迟到事件）
    ingested_at    timestamptz NOT NULL,   -- 入日志时间（服务端生成）
    type           text        NOT NULL CHECK (type IN (
                        'telemetry','violation','inspection','registry','work_order_created',
                        'work_order_closed','expert_assertion','correction',
                        'manual_override','projection_only')),
    payload        jsonb       NOT NULL,
    provenance     jsonb       NOT NULL,   -- {source_system, operator?}
    schema_version text        NOT NULL,
    PRIMARY KEY (tenant_id, event_id)      -- 分区键必须进主键
) PARTITION BY LIST (tenant_id);

-- 未注册租户的兜底分区（生产上仍建议 ensure_tenant_partition() 建独立分区）
CREATE TABLE IF NOT EXISTS event_log_default PARTITION OF event_log DEFAULT;

-- 对象内 seq 唯一（tenant_id 为分区键，已在索引首列）
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_log_object_seq
    ON event_log (tenant_id, object_id, seq);

-- 投影器读路径：按对象顺序扫描
CREATE INDEX IF NOT EXISTS ix_event_log_object_ingested
    ON event_log (tenant_id, object_id, ingested_at);

-- --------------------------------------------------------------------------
-- 2. 租户分区管理（幂等）
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ensure_tenant_partition(p_tenant text) RETURNS void AS $$
DECLARE
    part text := 'event_log_' || replace(p_tenant, '-', '_');
BEGIN
    IF to_regclass(format('public.%I', part)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF event_log FOR VALUES IN (%L)', part, p_tenant);
    END IF;
END $$ LANGUAGE plpgsql;

-- --------------------------------------------------------------------------
-- 3. seq 计数器（序列元数据，非日志；只允许本表 UPDATE）
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS object_seq (
    tenant_id text    NOT NULL,
    object_id text    NOT NULL,
    last_seq  bigint  NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    PRIMARY KEY (tenant_id, object_id)
);

-- --------------------------------------------------------------------------
-- 4. correction 目标存在性（区域规则 4：correction 链）
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION check_correction_target() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM event_log
        WHERE tenant_id = NEW.tenant_id
          AND event_id = (NEW.payload->>'corrects_event_id')::uuid
    ) THEN
        RAISE EXCEPTION
            'correction target % does not exist for tenant %',
            NEW.payload->>'corrects_event_id', NEW.tenant_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_event_log_correction_target ON event_log;
CREATE CONSTRAINT TRIGGER trg_event_log_correction_target
    AFTER INSERT ON event_log
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.type = 'correction')
    EXECUTE FUNCTION check_correction_target();

-- --------------------------------------------------------------------------
-- 5. 水位线日志（只前进）
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watermark_log (
    tenant_id   text        NOT NULL,
    object_id   text        NOT NULL,
    seq         bigint      NOT NULL CHECK (seq > 0),
    advanced_at timestamptz NOT NULL,   -- 写入方显式传入，不用 now() 默认值
    PRIMARY KEY (tenant_id, object_id, seq)
);

-- --------------------------------------------------------------------------
-- 6. outbox（事务内写入，relay 异步投递，消费端幂等）
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbox (
    id           bigserial   PRIMARY KEY,
    tenant_id    text        NOT NULL,
    event_id     uuid        NOT NULL,
    channel      text        NOT NULL,
    payload      jsonb       NOT NULL,
    status       text        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','delivered','dead')),
    attempts     integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error   text,
    created_at   timestamptz NOT NULL,
    delivered_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending
    ON outbox (status, id) WHERE status = 'pending';

-- --------------------------------------------------------------------------
-- 7. 权限：应用角色只有 SELECT/INSERT（日志），无 UPDATE/DELETE（铁律：只追加）
-- --------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'riskcore_app') THEN
        CREATE ROLE riskcore_app LOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO riskcore_app;
GRANT SELECT, INSERT ON event_log        TO riskcore_app;
GRANT SELECT, INSERT ON watermark_log    TO riskcore_app;
GRANT SELECT, INSERT, UPDATE ON object_seq TO riskcore_app;  -- 计数器元数据
GRANT SELECT, INSERT, UPDATE ON outbox   TO riskcore_app;
GRANT USAGE, SELECT ON SEQUENCE outbox_id_seq TO riskcore_app;

-- 显式撤销并文档化：日志本体永不可改（revoke 是 no-op 防御，授权本就未给）
REVOKE UPDATE, DELETE, TRUNCATE ON event_log FROM riskcore_app;
REVOKE UPDATE, DELETE, TRUNCATE ON watermark_log FROM riskcore_app;

COMMIT;
