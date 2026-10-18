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
-- The root approver is one exact identity: owner_principal + owner_account + owner_kind. Sharing the account does not
-- make another principal the root. A space with NULL owner_principal has no root until an operator sets it.
ALTER TABLE baton.spaces ADD COLUMN IF NOT EXISTS owner_principal text;
ALTER TABLE baton.spaces ADD COLUMN IF NOT EXISTS owner_kind text;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='baton.spaces'::regclass AND conname='space_owner_identity') THEN
        ALTER TABLE baton.spaces ADD CONSTRAINT space_owner_identity CHECK (
            (owner_principal IS NULL) = (owner_kind IS NULL) AND (owner_kind IS NULL OR owner_kind IN ('human','agent')));
    END IF;
END; $$;
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
-- Legacy values: the built-in worker was removed, so nothing writes execution_mode 'codex', gate_mode
-- 'worker_success', backend, a2a_id or dispatch_started_at any more. The check constraints keep admitting
-- them so old databases migrate unchanged and rows the worker wrote stay readable.
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
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS work_face jsonb;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS require_work_face boolean NOT NULL DEFAULT false;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS task_kind text NOT NULL DEFAULT 'construction' CHECK (task_kind IN ('construction','prep'));
-- Response-time agreements: deadlines apply only to assigned (manual-path) tasks; timestamps use database time.
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent','normal','low'));
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS respond_by timestamptz;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS responded_at timestamptz;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS escalated_at timestamptz;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS escalate_to text;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS fallback_assignee text;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS blocked_reason text CHECK (blocked_reason IN ('scope_conflict','lease_held','dependency_blocked','waiting_input','other'));
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS blocked_by text;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS blocked_at timestamptz;
CREATE INDEX IF NOT EXISTS tasks_overdue ON baton.tasks(respond_by) WHERE state IN ('queued','claimed') AND responded_at IS NULL AND escalated_at IS NULL AND respond_by IS NOT NULL;
CREATE TABLE IF NOT EXISTS baton.task_dependencies (
    task_id uuid NOT NULL REFERENCES baton.tasks(id),
    depends_on uuid NOT NULL REFERENCES baton.tasks(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(task_id,depends_on), CHECK (task_id <> depends_on)
);
CREATE INDEX IF NOT EXISTS dependency_reverse ON baton.task_dependencies(depends_on,task_id);
CREATE TABLE IF NOT EXISTS baton.x_claim (
    task_id uuid PRIMARY KEY REFERENCES baton.tasks(id), holder text NOT NULL, holder_account text NOT NULL,
    holder_kind text NOT NULL, epoch bigint NOT NULL, lease_expires_at timestamptz NOT NULL,
    scopes jsonb NOT NULL, scope_repository text NOT NULL DEFAULT 'global' CHECK (scope_repository = 'global')
);
-- The expiring-lease scan reads only claims whose expiry is inside the warning horizon, soonest first.
CREATE INDEX IF NOT EXISTS claim_lease_expiry ON baton.x_claim(lease_expires_at);
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
-- One expiry row per request. Reads compute expired and do not insert. The periodic sweep
-- (one task per short transaction, ordered by task id) and exclusive writes insert it.
CREATE UNIQUE INDEX IF NOT EXISTS audit_permission_expired_once
    ON baton.x_audit (task_id, (metadata->>'request_id'))
    WHERE event = 'permission.expired';

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

-- Asynchronous delivery: transactional notification outbox, webhooks and delivery ledger.
-- Rows carry identifiers only; no prompt, comment body or result text is ever stored here.
CREATE TABLE IF NOT EXISTS baton.x_notify (
    id uuid PRIMARY KEY, recipient text NOT NULL, recipient_account text NOT NULL, event text NOT NULL,
    object_type text NOT NULL CHECK (object_type IN ('task','page','comment')), object_id uuid NOT NULL,
    thread_id uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), seq bigserial NOT NULL UNIQUE
);
-- Who caused the notification; previews of content are computed at read time and are never stored.
ALTER TABLE baton.x_notify ADD COLUMN IF NOT EXISTS actor text;
ALTER TABLE baton.x_notify ADD COLUMN IF NOT EXISTS actor_kind text;
-- The result a task.awaiting_gate notification is about, so an approver can gate without another read.
-- An epoch number and a principal name: identifiers only, never result text.
ALTER TABLE baton.x_notify ADD COLUMN IF NOT EXISTS result_epoch bigint;
ALTER TABLE baton.x_notify ADD COLUMN IF NOT EXISTS result_author text;
CREATE INDEX IF NOT EXISTS notify_recipient ON baton.x_notify(recipient,seq);
CREATE TABLE IF NOT EXISTS baton.x_webhook (
    id uuid PRIMARY KEY, principal text NOT NULL, account text NOT NULL, url text NOT NULL,
    events jsonb, secret bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE baton.x_webhook ADD COLUMN IF NOT EXISTS include_preview boolean NOT NULL DEFAULT false;
-- Secret rotation: the replaced secret keeps signing until previous_secret_expires_at, then is cleared.
ALTER TABLE baton.x_webhook ADD COLUMN IF NOT EXISTS previous_secret bytea;
ALTER TABLE baton.x_webhook ADD COLUMN IF NOT EXISTS previous_secret_expires_at timestamptz;
-- Partial: only subscriptions inside a rotation grace period are indexed, so the periodic cleanup never scans the table.
CREATE INDEX IF NOT EXISTS webhook_previous_secret_expiry ON baton.x_webhook(previous_secret_expires_at) WHERE previous_secret_expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS webhook_principal ON baton.x_webhook(principal);
CREATE TABLE IF NOT EXISTS baton.x_webhook_delivery (
    subscription_id uuid NOT NULL REFERENCES baton.x_webhook(id) ON DELETE CASCADE,
    notification_id uuid NOT NULL REFERENCES baton.x_notify(id),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','delivered','dead')),
    attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
    PRIMARY KEY(subscription_id,notification_id)
);
CREATE INDEX IF NOT EXISTS webhook_delivery_due ON baton.x_webhook_delivery(next_attempt_at) WHERE state='pending';
CREATE TABLE IF NOT EXISTS baton.x_webhook_attempt (
    id uuid PRIMARY KEY, subscription_id uuid NOT NULL, notification_id uuid NOT NULL, attempt integer NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('delivered','http_error','network_error','refused','forbidden')),
    http_status integer, duration_ms integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(subscription_id,notification_id) REFERENCES baton.x_webhook_delivery(subscription_id,notification_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS webhook_attempt_delivery ON baton.x_webhook_attempt(subscription_id,notification_id,attempt);

-- Approver delegations (R1 approver gate). The space owner is the root approver and needs no row. "Approver" is
-- otherwise a role: no other principal, human or agent, may decide a gate without an active delegation covering the
-- task. A root delegation (no parent) is granted by the space owner (any kind), through the API or recorded by an
-- operator; a principal holding a may_delegate delegation can grant a narrower child (parent_id). Rows are
-- append-only apart from the single transition that records revoked_at and revoked_by, enforced for every role
-- including the schema owner.
CREATE TABLE IF NOT EXISTS baton.x_delegation (
    id uuid PRIMARY KEY,
    grantor text NOT NULL, grantor_account text NOT NULL, grantor_kind text NOT NULL CHECK (grantor_kind IN ('human','agent')),
    delegate text NOT NULL, delegate_account text NOT NULL, delegate_kind text NOT NULL,
    scope text NOT NULL CHECK (scope IN ('approve')),
    space_id uuid REFERENCES baton.spaces(id),
    may_delegate boolean NOT NULL DEFAULT false,
    parent_id uuid REFERENCES baton.x_delegation(id),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    created_via text NOT NULL CHECK (created_via IN ('api','operator')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz,
    revoked_by text,
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX IF NOT EXISTS delegation_delegate ON baton.x_delegation(delegate,delegate_account) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS delegation_grantor ON baton.x_delegation(grantor,grantor_account);
CREATE INDEX IF NOT EXISTS delegation_parent ON baton.x_delegation(parent_id) WHERE parent_id IS NOT NULL;
-- Who held the claim for each epoch (including a previous holder when work is handed off), so nobody can approve
-- work they executed. Append-only for the runtime role.
CREATE TABLE IF NOT EXISTS baton.x_claim_holder (
    task_id uuid NOT NULL REFERENCES baton.tasks(id), epoch bigint NOT NULL, holder text NOT NULL, holder_account text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(task_id,epoch,holder,holder_account)
);
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS delegation_id uuid REFERENCES baton.x_delegation(id);
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS grantor text;
-- root: decided by the space owner without a delegation. self_approval: the decider submitted or executed the work
-- (only the root may, and only with the explicit flag); it is never shown as someone else's approval.
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS root boolean NOT NULL DEFAULT false;
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS self_approval boolean NOT NULL DEFAULT false;
-- Approve with edits: an 'edit' decision is stored as a pass with edited=true, keeping the result as it was
-- submitted (original_result) beside the approver's replacement (edited_result) and the optional reason.
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS edited boolean NOT NULL DEFAULT false;
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS original_result text;
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS edited_result text;
ALTER TABLE baton.x_gate ADD COLUMN IF NOT EXISTS reason text;
-- Two-party confirmation. A policy matches a task through its declared scopes; the strictest match wins.
-- A task may raise (or, when its creator is the space owner, replace) the requirement at creation.
CREATE TABLE IF NOT EXISTS baton.x_approval_policy (
    space_id uuid NOT NULL REFERENCES baton.spaces(id), scope_kind text NOT NULL CHECK (scope_kind IN ('path','module','migration')),
    scope_value text NOT NULL, required_approvals integer NOT NULL CHECK (required_approvals BETWEEN 1 AND 2),
    root_required boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(space_id,scope_kind,scope_value)
);
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS approvals_required integer CHECK (approvals_required IN (1,2));
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS approvals_override_root boolean NOT NULL DEFAULT false;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='baton.x_gate'::regclass AND conname='gate_delegation_pair') THEN
        ALTER TABLE baton.x_gate ADD CONSTRAINT gate_delegation_pair CHECK ((delegation_id IS NULL) = (grantor IS NULL));
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='baton.x_gate'::regclass AND conname='gate_edit_pair') THEN
        ALTER TABLE baton.x_gate ADD CONSTRAINT gate_edit_pair CHECK (
            edited = (original_result IS NOT NULL AND edited_result IS NOT NULL AND decision='pass')
            AND (edited OR (original_result IS NULL AND edited_result IS NULL)));
    END IF;
END; $$;

CREATE OR REPLACE FUNCTION baton.guard_delegation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Baton delegations are append-only';
    END IF;
    IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL
       OR (to_jsonb(NEW) - 'revoked_at' - 'revoked_by') IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at' - 'revoked_by') THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Baton delegations are append-only';
    END IF;
    RETURN NEW;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.x_delegation'::regclass AND tgname='delegation_guard') THEN
        CREATE TRIGGER delegation_guard BEFORE UPDATE OR DELETE ON baton.x_delegation
            FOR EACH ROW EXECUTE FUNCTION baton.guard_delegation();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.x_delegation'::regclass AND tgname='delegation_no_truncate') THEN
        CREATE TRIGGER delegation_no_truncate BEFORE TRUNCATE ON baton.x_delegation
            FOR EACH STATEMENT EXECUTE FUNCTION baton.guard_delegation();
    END IF;
END; $$;

-- Backfill owner_principal/owner_kind for spaces created before they were recorded, from the space.created audit event
-- of the creating operation. Only an unambiguous event by the owning account is trusted; every other row stays NULL
-- (no root) until an operator runs `python -m bridge.delegations set-space-owner`. Idempotent: only NULL rows change.
UPDATE baton.spaces s SET owner_principal=a.agent, owner_kind=a.actor_kind
FROM (SELECT space_id, min(agent) AS agent, min(actor_kind) AS actor_kind, min(actor_account) AS actor_account
      FROM baton.x_audit WHERE event='space.created' AND space_id IS NOT NULL GROUP BY space_id HAVING count(*)=1) a
WHERE a.space_id=s.id AND s.owner_principal IS NULL AND a.actor_account=s.owner_account AND a.actor_kind IN ('human','agent');

-- A2A public interface (W2-1). Baton task states the task store cannot express are recorded here and projected onto
-- the A2A task state (see docs/design/space-replica.md, "Protocols" item 9). Only terminal overlays exist: a task
-- that was canceled (CancelTask) or rejected by its agent. One row per task, append-only for every role.
CREATE TABLE IF NOT EXISTS baton.x_a2a_task_state (
    task_id uuid PRIMARY KEY REFERENCES baton.tasks(id),
    state text NOT NULL CHECK (state IN ('canceled','rejected')),
    set_by text NOT NULL, set_by_account text NOT NULL, set_by_kind text NOT NULL CHECK (set_by_kind IN ('human','agent')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION baton.reject_a2a_state_change() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Baton A2A task states are append-only';
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.x_a2a_task_state'::regclass AND tgname='a2a_state_no_change') THEN
        CREATE TRIGGER a2a_state_no_change BEFORE UPDATE OR DELETE ON baton.x_a2a_task_state
            FOR EACH ROW EXECUTE FUNCTION baton.reject_a2a_state_change();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='baton.x_a2a_task_state'::regclass AND tgname='a2a_state_no_truncate') THEN
        CREATE TRIGGER a2a_state_no_truncate BEFORE TRUNCATE ON baton.x_a2a_task_state
            FOR EACH STATEMENT EXECUTE FUNCTION baton.reject_a2a_state_change();
    END IF;
END; $$;

-- Role queues. A task may carry a role (explicit, or derived from the space's role rules at creation). A principal holds
-- zero or more roles per space; only the root or a covering approver delegation assigns them, and every change is in
-- x_audit. The rules and the per-space SLA map live in x_role_config (NULL sla_minutes means the built-in defaults).
-- role_sla_notified_at is the last time a role.sla_breached notification was written for the task.
-- role_assign_authority is set only when the space root, or an approver whose delegation covers the space,
-- names an assignee who does not hold the task's role. A non-holder may claim that task only when they are
-- that assignee and this column is set. NULL means the assignment carries no such grant.
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS role text CHECK (role ~ '^[a-z][a-z0-9_.-]{0,63}$');
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS role_source text CHECK (role_source IN ('explicit','derived'));
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS role_sla_notified_at timestamptz;
ALTER TABLE baton.tasks ADD COLUMN IF NOT EXISTS role_assign_authority text;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='baton.tasks'::regclass AND conname='task_role_source_pair') THEN
        ALTER TABLE baton.tasks ADD CONSTRAINT task_role_source_pair CHECK ((role IS NULL) = (role_source IS NULL));
    END IF;
END; $$;
CREATE INDEX IF NOT EXISTS tasks_role_queue ON baton.tasks(role,created_at) WHERE role IS NOT NULL AND state IN ('queued','claimed');
CREATE TABLE IF NOT EXISTS baton.x_role_holder (
    space_id uuid NOT NULL REFERENCES baton.spaces(id),
    role text NOT NULL CHECK (role ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    holder text NOT NULL, holder_account text NOT NULL, holder_kind text NOT NULL CHECK (holder_kind IN ('human','agent')),
    granted_by text NOT NULL, authority text NOT NULL, delegation_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(space_id,role,holder,holder_account,holder_kind)
);
CREATE INDEX IF NOT EXISTS role_holder_identity ON baton.x_role_holder(holder,holder_account,holder_kind);
CREATE TABLE IF NOT EXISTS baton.x_role_config (
    space_id uuid PRIMARY KEY REFERENCES baton.spaces(id),
    rules jsonb NOT NULL DEFAULT '[]', sla_minutes jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_by text NOT NULL
);
