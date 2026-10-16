-- SPDX-License-Identifier: Apache-2.0
-- Run transactionally as baton_owner. Runtime baton_app must never own these objects.
CREATE SCHEMA IF NOT EXISTS baton;
CREATE TABLE IF NOT EXISTS baton.schema_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), version integer NOT NULL CHECK (version = 1)
);
INSERT INTO baton.schema_version(singleton,version) VALUES (true,1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS baton.spaces (
    id uuid PRIMARY KEY, name text NOT NULL, owner_account text NOT NULL,
    scope_repository text NOT NULL DEFAULT 'global' CHECK (scope_repository = 'global'),
    default_for text UNIQUE, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS baton.pages (
    id uuid PRIMARY KEY, space_id uuid NOT NULL REFERENCES baton.spaces(id),
    parent_id uuid REFERENCES baton.pages(id), owner_account text NOT NULL, title text NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), CHECK (parent_id IS DISTINCT FROM id)
);
CREATE TABLE IF NOT EXISTS baton.blocks (
    id uuid PRIMARY KEY, page_id uuid NOT NULL REFERENCES baton.pages(id),
    kind text NOT NULL CHECK (kind IN ('text','file','prompt','agent_instructions','task')),
    content jsonb NOT NULL, position bigint NOT NULL, revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    author_principal text NOT NULL, author_account text NOT NULL, author_kind text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS baton.block_revisions (
    block_id uuid NOT NULL REFERENCES baton.blocks(id), revision bigint NOT NULL, content jsonb NOT NULL,
    author_principal text NOT NULL, author_account text NOT NULL, author_kind text NOT NULL,
    operation_id uuid NOT NULL, proposal_id uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(block_id,revision)
);
CREATE TABLE IF NOT EXISTS baton.teams (
    id uuid PRIMARY KEY, name text NOT NULL, owner_account text NOT NULL
);
CREATE TABLE IF NOT EXISTS baton.team_members (
    team_id uuid NOT NULL REFERENCES baton.teams(id), account text NOT NULL, PRIMARY KEY(team_id,account)
);
CREATE TABLE IF NOT EXISTS baton.grants (
    resource_type text NOT NULL CHECK (resource_type IN ('space','page')), resource_id uuid NOT NULL,
    subject_type text NOT NULL CHECK (subject_type IN ('person','team')), subject_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('view','comment','edit')),
    PRIMARY KEY(resource_type,resource_id,subject_type,subject_id)
);
CREATE TABLE IF NOT EXISTS baton.comments (
    id uuid PRIMARY KEY, page_id uuid NOT NULL REFERENCES baton.pages(id), thread_id uuid NOT NULL,
    body text NOT NULL, selection jsonb, subject text NOT NULL DEFAULT 'Comment',
    author_principal text NOT NULL, author_account text NOT NULL, author_kind text NOT NULL,
    requested_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS comments_thread ON baton.comments(thread_id,created_at);
CREATE TABLE IF NOT EXISTS baton.mentions (
    id uuid PRIMARY KEY, comment_id uuid NOT NULL REFERENCES baton.comments(id),
    target_type text NOT NULL CHECK (target_type IN ('person','agent')), target_id text NOT NULL,
    action_requested boolean NOT NULL, requested_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT mentions_recipient_requester_key UNIQUE(comment_id,target_type,target_id,requested_by)
);
ALTER TABLE baton.mentions ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE baton.mentions DROP CONSTRAINT IF EXISTS mentions_comment_id_target_type_target_id_key;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='baton.mentions'::regclass AND conname='mentions_recipient_requester_key') THEN
        ALTER TABLE baton.mentions ADD CONSTRAINT mentions_recipient_requester_key UNIQUE(comment_id,target_type,target_id,requested_by);
    END IF;
END; $$;
CREATE INDEX IF NOT EXISTS mentions_recipient ON baton.mentions(target_type,target_id);
CREATE TABLE IF NOT EXISTS baton.proposals (
    id uuid PRIMARY KEY, block_id uuid NOT NULL REFERENCES baton.blocks(id), expected_revision bigint NOT NULL,
    proposed_content jsonb NOT NULL, selection jsonb, status text NOT NULL DEFAULT 'pending',
    revision bigint NOT NULL DEFAULT 1, last_decision text,
    author_principal text NOT NULL, author_account text NOT NULL, author_kind text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS baton.proposal_revisions (
    proposal_id uuid NOT NULL REFERENCES baton.proposals(id), revision bigint NOT NULL, content jsonb NOT NULL,
    author_principal text NOT NULL, author_account text NOT NULL, author_kind text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(proposal_id,revision)
);
CREATE TABLE IF NOT EXISTS baton.tasks (
    id uuid PRIMARY KEY, page_id uuid NOT NULL REFERENCES baton.pages(id), block_id uuid NOT NULL UNIQUE REFERENCES baton.blocks(id),
    owner text NOT NULL, owner_account text NOT NULL, owner_kind text NOT NULL, assignee text,
    idempotency_key text, input_hash text NOT NULL, prompt text NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('manual','codex')),
    gate_mode text NOT NULL CHECK (gate_mode IN ('reviewer','worker_success')),
    reviewers jsonb NOT NULL DEFAULT '[]', scopes jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','claimed','awaiting_gate','done','failed','interrupted')),
    epoch bigint NOT NULL DEFAULT 0, result_epoch bigint, result text, result_author text, result_author_account text,
    base_commit text, context jsonb NOT NULL DEFAULT '[]', evidence jsonb NOT NULL DEFAULT '[]',
    a2a_id uuid, backend text CHECK (backend IN ('demo','codex')), dispatch_started_at timestamptz,
    error text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(owner,idempotency_key)
);
CREATE INDEX IF NOT EXISTS tasks_queue ON baton.tasks(execution_mode,state,created_at);
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS assignee text;
CREATE TABLE IF NOT EXISTS baton.x_claim (
    task_id uuid PRIMARY KEY REFERENCES baton.tasks(id), holder text NOT NULL, holder_account text NOT NULL,
    holder_kind text NOT NULL, epoch bigint NOT NULL, lease_expires_at timestamptz NOT NULL,
    scopes jsonb NOT NULL, scope_repository text NOT NULL DEFAULT 'global' CHECK (scope_repository = 'global')
);
CREATE TABLE IF NOT EXISTS baton.x_handoff (
    id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES baton.tasks(id), from_holder text NOT NULL, to_holder text NOT NULL,
    old_epoch bigint NOT NULL, new_epoch bigint NOT NULL, base_commit text, context jsonb NOT NULL, evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS baton.x_gate (
    id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES baton.tasks(id), epoch bigint NOT NULL,
    decision text NOT NULL CHECK (decision IN ('pass','fail')), reviewer text NOT NULL, reviewer_account text NOT NULL,
    evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS baton.operations (
    principal text NOT NULL, operation_id uuid NOT NULL, request_hash text NOT NULL,
    method text NOT NULL, result jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(principal,operation_id)
);
CREATE TABLE IF NOT EXISTS baton.x_audit (
    id uuid PRIMARY KEY, time timestamptz NOT NULL DEFAULT clock_timestamp(), event text NOT NULL,
    agent text NOT NULL, actor_account text NOT NULL, actor_kind text NOT NULL,
    requested_by text NOT NULL, performed_by text NOT NULL, operation_id uuid NOT NULL,
    space_id uuid, task_id uuid, metadata jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_task ON baton.x_audit(task_id,time);
CREATE INDEX IF NOT EXISTS audit_space ON baton.x_audit(space_id,time);

CREATE OR REPLACE FUNCTION baton.reject_audit_change() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Baton audit is append-only';
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.x_audit'::regclass AND tgname='audit_no_change') THEN
        CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON baton.x_audit
            FOR EACH ROW EXECUTE FUNCTION baton.reject_audit_change();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.x_audit'::regclass AND tgname='audit_no_truncate') THEN
        CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON baton.x_audit
            FOR EACH STATEMENT EXECUTE FUNCTION baton.reject_audit_change();
    END IF;
END; $$;

CREATE OR REPLACE FUNCTION baton.check_page_parent() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE parent_space uuid; owner_name text; BEGIN
    SELECT owner_account INTO owner_name FROM baton.spaces WHERE id=NEW.space_id;
    IF NEW.owner_account IS DISTINCT FROM owner_name THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid page owner';
    END IF;
    IF NEW.parent_id IS NOT NULL THEN
        SELECT space_id INTO parent_space FROM baton.pages WHERE id=NEW.parent_id;
        IF parent_space IS DISTINCT FROM NEW.space_id OR EXISTS (
            WITH RECURSIVE parents AS (
                SELECT id,parent_id FROM baton.pages WHERE id=NEW.parent_id
                UNION SELECT p.id,p.parent_id FROM baton.pages p JOIN parents a ON p.id=a.parent_id
            ) SELECT 1 FROM parents WHERE id=NEW.id
        ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid page parent'; END IF;
    END IF;
    RETURN NEW;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.pages'::regclass AND tgname='page_parent_check') THEN
        CREATE TRIGGER page_parent_check BEFORE INSERT OR UPDATE OF parent_id,space_id,owner_account ON baton.pages
            FOR EACH ROW EXECUTE FUNCTION baton.check_page_parent();
    END IF;
END; $$;
