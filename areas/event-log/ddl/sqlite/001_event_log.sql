-- ============================================================================
-- 001_event_log.sql — fact log schema (SQLite, development/test store)
--
-- 与 ddl/postgres/001_event_log.sql 语义等价。差异及原因：
--   * SQLite 无角色系统，"禁 UPDATE/DELETE" 由 RAISE(ABORT) 触发器强制
--     （开发态防护）。生产环境以 Postgres GRANT 为准，那才是真权限边界。
--   * payload/provenance 以规范化 JSON 文本存储（sort_keys，紧凑分隔符），
--     保证黄金重放的字节级一致性。
--   * seq 由 object_seq 计数器在写事务内分配（UPDATE ... RETURNING）。
--   * occurred_at/ingested_at 统一存 UTC ISO-8601 字符串。
-- ============================================================================

CREATE TABLE IF NOT EXISTS event_log (
    tenant_id      TEXT NOT NULL,
    event_id       TEXT NOT NULL,               -- uuid 字符串
    trace_id       TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    object_type    TEXT NOT NULL CHECK (object_type IN (
                        'driver','vehicle','route','cargo','transport_task','enterprise')),
    seq            INTEGER NOT NULL CHECK (seq > 0),
    occurred_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL,
    type           TEXT NOT NULL CHECK (type IN (
                        'telemetry','violation','inspection','registry','work_order_created',
                        'work_order_closed','expert_assertion','correction',
                        'manual_override','projection_only')),
    payload        TEXT NOT NULL,               -- 规范化 JSON
    provenance     TEXT NOT NULL,               -- 规范化 JSON
    schema_version TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_log_object_seq
    ON event_log (tenant_id, object_id, seq);

CREATE INDEX IF NOT EXISTS ix_event_log_object_ingested
    ON event_log (tenant_id, object_id, ingested_at);

-- R2：只追加。开发态防护触发器（生产态 = Postgres GRANT）。
CREATE TRIGGER IF NOT EXISTS trg_event_log_no_update
BEFORE UPDATE ON event_log
BEGIN
    SELECT RAISE(ABORT, 'event_log is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_log_no_delete
BEFORE DELETE ON event_log
BEGIN
    SELECT RAISE(ABORT, 'event_log is append-only: DELETE rejected');
END;

-- R3：seq 计数器（元数据，允许 UPDATE；日志本体不允许）
CREATE TABLE IF NOT EXISTS object_seq (
    tenant_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    last_seq  INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    PRIMARY KEY (tenant_id, object_id)
);

-- 水位线日志（只前进）
CREATE TABLE IF NOT EXISTS watermark_log (
    tenant_id   TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    seq         INTEGER NOT NULL CHECK (seq > 0),
    advanced_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, object_id, seq)
);

-- outbox（事务内写入，relay 异步投递）
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    channel      TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','delivered','dead')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_outbox_pending
    ON outbox (status, id) WHERE status = 'pending';
