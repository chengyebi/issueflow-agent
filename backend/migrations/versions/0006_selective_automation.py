"""选择性自动化：解耦人工审核与 GitHub 命令授权。"""

from alembic import op

revision = "0006_selective_automation"
down_revision = "0005_issue_chunk_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- 1) 新的自动化裁定表：每个 Agent Run 至多一条。
        CREATE TABLE IF NOT EXISTS automation_decisions (
            id SERIAL PRIMARY KEY,
            agent_run_id INTEGER NOT NULL UNIQUE REFERENCES agent_runs(id),
            disposition TEXT NOT NULL
                CHECK (disposition IN ('auto_execute', 'defer', 'no_action')),
            policy_version TEXT NOT NULL,
            shadow BOOLEAN NOT NULL DEFAULT FALSE,
            reason_code TEXT,
            reason TEXT,
            human_task TEXT,
            evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            already_checked JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_automation_decisions_agent_run_id
            ON automation_decisions (agent_run_id);
        CREATE INDEX IF NOT EXISTS idx_automation_decisions_disposition
            ON automation_decisions (disposition);
        CREATE INDEX IF NOT EXISTS idx_automation_decisions_reason_code
            ON automation_decisions (reason_code);
        """
    )
    # 2) github_commands 解耦：agent_run_id 可空列 + review_task_id 改为可空。
    op.execute(
        """
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS agent_run_id INTEGER REFERENCES agent_runs(id);
        ALTER TABLE github_commands
            ALTER COLUMN review_task_id DROP NOT NULL;

        -- 3) 从旧 review_task 关系回填 agent_run_id。
        UPDATE github_commands gc
        SET agent_run_id = rt.agent_run_id
        FROM review_tasks rt
        WHERE gc.agent_run_id IS NULL
          AND gc.review_task_id = rt.id;

        -- 4) 授权来源与策略元数据。
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS authorization_source TEXT
                CHECK (authorization_source IN ('human', 'policy'));
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS authorization_reason TEXT;
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS policy_version TEXT;
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS action_intent TEXT;
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS action_confidence DOUBLE PRECISION;
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS rationale TEXT;
        ALTER TABLE github_commands
            ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '[]'::jsonb;

        -- 5) 旧数据回填：有 review_task 的命令标记为 human 授权。
        UPDATE github_commands
        SET authorization_source = 'human'
        WHERE authorization_source IS NULL
          AND review_task_id IS NOT NULL;

        -- 6) 保留旧索引，新增 agent_run_id 索引便于按 run 查询。
        CREATE INDEX IF NOT EXISTS idx_github_commands_agent_run_id
            ON github_commands (agent_run_id);
        CREATE INDEX IF NOT EXISTS idx_github_commands_authorization_source
            ON github_commands (authorization_source);
        """
    )
    # 7) outbox_events 增加 github_commands 事件类型（原 CHECK 约束）。
    op.execute(
        """
        ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_event_type_check;
        ALTER TABLE outbox_events
            ADD CONSTRAINT outbox_events_event_type_check
            CHECK (event_type IN ('agent_run', 'github_commands', 'review_commands', 'issue_index'));
        """
    )


def downgrade() -> None:
    # 保留历史数据；本迁移的可空化与回填不具破坏性，不实现降级。
    pass
