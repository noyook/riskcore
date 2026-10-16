-- ============================================================================
-- 001_executor.sql — executor-local state (SQLite)
-- intervention_state: 滞后带状态机（决策层记忆，D-002；不是日志）
-- bypass_observation: 旁路观察期"本会拒派"留痕（W-006，喂给 W-009 双跑比对）
-- ============================================================================

CREATE TABLE IF NOT EXISTS intervention_state (
    tenant_id     TEXT NOT NULL DEFAULT 't1',
    object_id     TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    intervention  TEXT NOT NULL CHECK (intervention IN ('none','alert','restrict','lock')),
    r_at_entry    INTEGER,
    since         TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, object_id, predicate)
);

CREATE TABLE IF NOT EXISTS bypass_observation (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id          TEXT NOT NULL,
    predicate          TEXT NOT NULL,
    would_intervention TEXT NOT NULL,
    reason_code        TEXT NOT NULL,
    r_milli            INTEGER,
    clause             TEXT NOT NULL,
    explain_refs       TEXT NOT NULL,
    state_as_of        TEXT,
    observed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_observation_object ON bypass_observation (object_id, id);
