-- Tenancy foundation (DECISIONS #165, #166): organizations (tenants), who
-- belongs to which org in what role, time-limited consented vendor support
-- access, and an append-only audit log written only by the database itself.
--
-- Trust model, stated plainly:
-- * The API identifies the caller to the database by setting the session
--   variable app.user_id (app/auth/context.py) after verifying their token.
--   Every grant rule below keys off that value, never off a column the API
--   supplies (granted_by, consented_by, actor are stamped here).
-- * These rules stop application LOGIC bugs from granting access, skipping
--   audit, or leaving an org without an admin. They do not defend against
--   someone who can run arbitrary SQL as app_runtime (e.g. via SQL
--   injection): that attacker could set app.user_id to anyone. All SQL in
--   the app stays parameterized.
-- * The privileged role (login roles in app_privileged: migrations, the
--   admin CLI) may make changes the rules otherwise forbid (recovery,
--   bootstrap), but only when it names a reason in app.system_job, and that
--   reason is recorded in the audit row. Changes that record a person
--   (granted_by, revoked_by, created_by) also need app.user_id set to the
--   real operator; a reason alone suffices only for audit-only changes
--   (users, org status/billing).
-- * Grant checks assume READ COMMITTED (the default). The guards refuse to
--   run under REPEATABLE READ/SERIALIZABLE, where a stale snapshot could
--   let two concurrent revocations remove an org's last admin.

-- ---------------------------------------------------------------------------
-- Identity helpers
-- ---------------------------------------------------------------------------

-- Privileged sessions are an explicit allowlist: login roles that are
-- members of app_privileged (the migration/admin role). Everything else,
-- app_runtime included, is unprivileged. session_user (not current_user),
-- because inside SECURITY DEFINER functions current_user is the owner.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_privileged') THEN
        CREATE ROLE app_privileged NOLOGIN;
    END IF;
END $$;
GRANT app_privileged TO CURRENT_USER;

CREATE FUNCTION app_is_privileged_session() RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT pg_has_role(session_user, 'app_privileged', 'MEMBER') $$;

-- The acting person for this request, or NULL if none was set.
CREATE FUNCTION app_actor() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
$$;

-- The acting person; raises if none was set.
CREATE FUNCTION app_current_user_id() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE v uuid := app_actor();
BEGIN
    IF v IS NULL THEN
        RAISE EXCEPTION 'no acting user on this connection (app.user_id not set)'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN v;
END $$;

-- A reason for a privileged change, honored only on a privileged session.
CREATE FUNCTION app_system_job() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN app_is_privileged_session()
                THEN NULLIF(btrim(current_setting('app.system_job', true)), '') END
$$;

CREATE FUNCTION app_require_read_committed() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'tenancy changes must run at READ COMMITTED (got %)', current_setting('transaction_isolation')
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE organizations (
    org_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name TEXT NOT NULL CHECK (btrim(legal_name) <> ''),
    display_name TEXT,
    -- active <-> suspended -> closed (terminal).
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'closed')),
    -- Who pays. Separate from tenancy: one billing account may cover many
    -- orgs. No billing table yet; unused until billing exists.
    billing_account_id UUID,
    created_by UUID NOT NULL REFERENCES users (user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE org_memberships (
    membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations (org_id),
    user_id UUID NOT NULL REFERENCES users (user_id),
    role TEXT NOT NULL CHECK (role IN ('org_admin', 'point_person', 'contributor', 'budget_owner', 'authorized_approver')),
    granted_by UUID NOT NULL REFERENCES users (user_id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by UUID REFERENCES users (user_id),
    CHECK (granted_by <> user_id),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);
CREATE UNIQUE INDEX org_memberships_one_active
    ON org_memberships (org_id, user_id, role) WHERE revoked_at IS NULL;
CREATE INDEX org_memberships_user ON org_memberships (user_id) WHERE revoked_at IS NULL;

CREATE TABLE support_access_grants (
    grant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations (org_id),
    support_user_id UUID NOT NULL REFERENCES users (user_id),
    consented_by UUID NOT NULL REFERENCES users (user_id),
    consented_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_by UUID REFERENCES users (user_id),
    CHECK (consented_by <> support_user_id),
    CHECK (expires_at > starts_at AND expires_at <= starts_at + interval '14 days'),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX support_access_grants_org ON support_access_grants (org_id) WHERE revoked_at IS NULL;

CREATE TABLE audit_log (
    audit_id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL only for privileged system jobs, which then carry detail.system_job.
    actor_user_id UUID REFERENCES users (user_id),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    org_id UUID REFERENCES organizations (org_id),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (actor_user_id IS NOT NULL OR detail ? 'system_job')
);
CREATE INDEX audit_log_org_at ON audit_log (org_id, at);

-- ---------------------------------------------------------------------------
-- Shared membership checks (SECURITY DEFINER so they see every row
-- regardless of future row-level security, and so Phase C policies can call
-- them without recursing into org_memberships' own policy).
-- ---------------------------------------------------------------------------

CREATE FUNCTION app_user_active(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM users WHERE user_id = p_user AND disabled_at IS NULL)
$$;

CREATE FUNCTION app_is_platform_admin(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM users WHERE user_id = p_user AND disabled_at IS NULL AND is_platform_admin)
$$;

-- Active membership: not revoked, the person is enabled, the org is active,
-- and the person is NOT a platform admin. Vendor staff never act through a
-- membership, even one left over from before a promotion; their only way
-- in is a support grant.
CREATE FUNCTION app_is_member(p_org uuid, p_user uuid, p_roles text[]) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM org_memberships m
        JOIN users u ON u.user_id = m.user_id
        JOIN organizations o ON o.org_id = m.org_id
        WHERE m.org_id = p_org AND m.user_id = p_user AND m.role = ANY (p_roles)
          AND m.revoked_at IS NULL AND u.disabled_at IS NULL AND NOT u.is_platform_admin
          AND o.status = 'active'
    )
$$;

-- Active vendor support access to an org right now.
CREATE FUNCTION app_active_support_grant(p_org uuid, p_user uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1
        FROM support_access_grants g
        JOIN organizations o ON o.org_id = g.org_id
        WHERE g.org_id = p_org AND g.support_user_id = p_user
          AND g.revoked_at IS NULL AND g.starts_at <= now() AND g.expires_at > now()
          AND o.status = 'active'
          AND app_is_platform_admin(p_user)
    )
$$;

-- ---------------------------------------------------------------------------
-- Audit writing: the only way rows get into audit_log.
-- ---------------------------------------------------------------------------

CREATE FUNCTION audit_write(p_action text, p_object_type text, p_object_id text, p_org uuid, p_detail jsonb)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_actor uuid := app_actor();
    v_job text := app_system_job();
    v_detail jsonb := COALESCE(p_detail, '{}'::jsonb) - 'system_job' - 'source';
    v_from_trigger boolean := pg_trigger_depth() > 0;
BEGIN
    IF NOT app_is_privileged_session() THEN
        -- The API must always act as a person.
        v_actor := app_current_user_id();
        IF NOT v_from_trigger THEN
            -- API actions use a strict lowercase "area.event" format, and the
            -- areas the database records itself are off limits.
            IF p_action !~ '^[a-z_]+\.[a-z_]+$' THEN
                RAISE EXCEPTION 'audit action % must look like "area.event" in lowercase', p_action
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF split_part(p_action, '.', 1) IN ('org', 'membership', 'support_access', 'user') THEN
                RAISE EXCEPTION 'audit action % is reserved for the database', p_action USING ERRCODE = 'insufficient_privilege';
            END IF;
            -- The API may only log into an org's trail if the actor belongs there.
            IF p_org IS NOT NULL AND NOT (app_is_member(p_org, v_actor,
                    ARRAY['org_admin','point_person','contributor','budget_owner','authorized_approver'])
                    OR app_active_support_grant(p_org, v_actor)) THEN
                RAISE EXCEPTION 'cannot write to that organization''s audit trail' USING ERRCODE = 'insufficient_privilege';
            END IF;
        END IF;
        v_detail := v_detail || jsonb_build_object('source', CASE WHEN v_from_trigger THEN 'database' ELSE 'api' END);
    ELSIF v_job IS NOT NULL THEN
        v_detail := v_detail || jsonb_build_object('system_job', v_job, 'source', 'system');
    ELSIF v_actor IS NULL THEN
        RAISE EXCEPTION 'privileged change without an actor or app.system_job reason'
            USING ERRCODE = 'insufficient_privilege';
    ELSE
        v_detail := v_detail || jsonb_build_object('source', 'operator');
    END IF;
    INSERT INTO audit_log (actor_user_id, action, object_type, object_id, org_id, detail)
    VALUES (v_actor, p_action, p_object_type, p_object_id, p_org, v_detail);
END $$;

-- Returns {"col": [old, new], ...} for the named columns that changed.
CREATE FUNCTION app_changed_columns(p_old jsonb, p_new jsonb, p_cols text[]) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(jsonb_object_agg(c, jsonb_build_array(p_old -> c, p_new -> c)), '{}'::jsonb)
    FROM unnest(p_cols) AS c
    WHERE (p_old -> c) IS DISTINCT FROM (p_new -> c)
$$;

-- ---------------------------------------------------------------------------
-- organizations rules + audit
-- ---------------------------------------------------------------------------

CREATE FUNCTION trg_organizations_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid;
    v_job boolean := app_system_job() IS NOT NULL;
BEGIN
    IF TG_OP = 'INSERT' THEN
        me := app_current_user_id();  -- an org is always created by a named person
        IF NOT v_job THEN
            IF NOT app_is_platform_admin(me) THEN
                RAISE EXCEPTION 'only a platform admin can create an organization' USING ERRCODE = 'insufficient_privilege';
            END IF;
            NEW.status := 'active';            -- new orgs always start active
            NEW.billing_account_id := NULL;    -- billing is set only by the privileged path
        END IF;
        NEW.created_by := me;
        NEW.created_at := now();
        RETURN NEW;
    END IF;

    -- UPDATE. Identity columns never change; legal_name only with a reason.
    IF (NEW.org_id <> OLD.org_id) OR (NEW.created_by <> OLD.created_by) OR (NEW.created_at <> OLD.created_at) THEN
        RAISE EXCEPTION 'organization identity fields cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.legal_name <> OLD.legal_name AND NOT v_job THEN
        RAISE EXCEPTION 'legal name changes need the privileged path' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'closed' THEN
            RAISE EXCEPTION 'a closed organization cannot be reopened' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT v_job AND NOT app_is_platform_admin(app_current_user_id()) THEN
            RAISE EXCEPTION 'only a platform admin can change an organization''s status' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF NEW.display_name IS DISTINCT FROM OLD.display_name AND NOT v_job THEN
        IF NOT app_is_member(OLD.org_id, app_current_user_id(), ARRAY['org_admin']) THEN
            RAISE EXCEPTION 'only an org admin can rename the organization' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF NEW.billing_account_id IS DISTINCT FROM OLD.billing_account_id AND NOT v_job THEN
        RAISE EXCEPTION 'billing can only be changed by the privileged path' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION trg_organizations_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit_write('org.create', 'organization', NEW.org_id::text, NEW.org_id,
            jsonb_build_object('legal_name', NEW.legal_name, 'status', NEW.status));
    ELSE
        PERFORM audit_write('org.update', 'organization', NEW.org_id::text, NEW.org_id,
            jsonb_build_object('changed', app_changed_columns(to_jsonb(OLD), to_jsonb(NEW),
                ARRAY['legal_name', 'display_name', 'status', 'billing_account_id'])));
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER organizations_guard BEFORE INSERT OR UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION trg_organizations_guard();
CREATE TRIGGER organizations_audit AFTER INSERT OR UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION trg_organizations_audit();

-- ---------------------------------------------------------------------------
-- org_memberships rules + audit
-- ---------------------------------------------------------------------------

CREATE FUNCTION trg_org_memberships_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_status text;
    v_privileged boolean := app_system_job() IS NOT NULL;
    v_other_admins int;
BEGIN
    PERFORM app_require_read_committed();
    -- Serialize all membership changes for this org (no grant/revoke races).
    SELECT status INTO v_status FROM organizations WHERE org_id = COALESCE(NEW.org_id, OLD.org_id) FOR UPDATE;

    IF TG_OP = 'INSERT' THEN
        NEW.granted_by := me;
        NEW.granted_at := now();
        IF NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
            RAISE EXCEPTION 'a new membership cannot start revoked' USING ERRCODE = 'check_violation';
        END IF;
        IF v_privileged THEN
            RETURN NEW;  -- recovery/bootstrap via the privileged CLI, audited with its reason
        END IF;
        IF NOT app_user_active(me) THEN
            RAISE EXCEPTION 'your account is not active' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT app_user_active(NEW.user_id) THEN
            RAISE EXCEPTION 'that person''s account is not active' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.user_id = me THEN
            RAISE EXCEPTION 'you cannot grant yourself access' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF v_status IS DISTINCT FROM 'active' THEN
            RAISE EXCEPTION 'the organization is not active' USING ERRCODE = 'check_violation';
        END IF;
        -- Vendor staff (platform admins) never become members of a customer
        -- org; their only way in is consented, time-limited support access.
        IF app_is_platform_admin(NEW.user_id) THEN
            RAISE EXCEPTION 'platform admins get access only through support grants, not memberships'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.role = 'org_admin' THEN
            IF app_is_member(NEW.org_id, me, ARRAY['org_admin']) THEN
                RETURN NEW;
            END IF;
            -- A platform admin may appoint an org admin only when the org has
            -- no ACTIVE admin (first setup, or recovery after the only admin
            -- was disabled).
            IF app_is_platform_admin(me)
               AND NOT EXISTS (SELECT 1 FROM org_memberships m JOIN users u ON u.user_id = m.user_id
                               WHERE m.org_id = NEW.org_id AND m.role = 'org_admin'
                                 AND m.revoked_at IS NULL AND u.disabled_at IS NULL) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'not allowed to grant org admin here' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF app_is_member(NEW.org_id, me, ARRAY['org_admin', 'point_person']) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'not allowed to grant roles in this organization' USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- UPDATE: the only allowed change is revoking an active membership.
    IF NEW.membership_id <> OLD.membership_id OR NEW.org_id <> OLD.org_id OR NEW.user_id <> OLD.user_id
       OR NEW.role <> OLD.role OR NEW.granted_by <> OLD.granted_by OR NEW.granted_at <> OLD.granted_at THEN
        RAISE EXCEPTION 'only revocation may change a membership' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'membership already revoked; grant a new one instead' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'only revocation may change a membership' USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.revoked_by := me;
    NEW.revoked_at := now();
    IF v_privileged THEN
        RETURN NEW;
    END IF;
    IF NOT (app_is_member(OLD.org_id, me, ARRAY['org_admin']) OR (OLD.user_id = me AND app_user_active(me))) THEN
        RAISE EXCEPTION 'not allowed to remove this membership' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.role = 'org_admin' THEN
        SELECT count(*) INTO v_other_admins
        FROM org_memberships m JOIN users u ON u.user_id = m.user_id
        WHERE m.org_id = OLD.org_id AND m.role = 'org_admin' AND m.revoked_at IS NULL
          AND u.disabled_at IS NULL AND m.membership_id <> OLD.membership_id;
        IF v_other_admins = 0 THEN
            RAISE EXCEPTION 'an organization must keep at least one active admin' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION trg_org_memberships_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit_write('membership.grant', 'membership', NEW.membership_id::text, NEW.org_id,
            jsonb_build_object('target_user_id', NEW.user_id, 'role', NEW.role));
    ELSE
        PERFORM audit_write('membership.revoke', 'membership', NEW.membership_id::text, NEW.org_id,
            jsonb_build_object('target_user_id', NEW.user_id, 'role', NEW.role,
                               'self_revoked', NEW.revoked_by = NEW.user_id));
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER org_memberships_guard BEFORE INSERT OR UPDATE ON org_memberships
    FOR EACH ROW EXECUTE FUNCTION trg_org_memberships_guard();
CREATE TRIGGER org_memberships_audit AFTER INSERT OR UPDATE ON org_memberships
    FOR EACH ROW EXECUTE FUNCTION trg_org_memberships_audit();

-- ---------------------------------------------------------------------------
-- support_access_grants rules + audit
-- ---------------------------------------------------------------------------

CREATE FUNCTION trg_support_access_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_status text;
BEGIN
    PERFORM app_require_read_committed();
    SELECT status INTO v_status FROM organizations WHERE org_id = COALESCE(NEW.org_id, OLD.org_id) FOR UPDATE;

    IF TG_OP = 'INSERT' THEN
        IF NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
            RAISE EXCEPTION 'a new grant cannot start revoked' USING ERRCODE = 'check_violation';
        END IF;
        -- Consent comes from the customer's own admin, never from the vendor.
        IF v_status IS DISTINCT FROM 'active' THEN
            RAISE EXCEPTION 'the organization is not active' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT app_is_member(NEW.org_id, me, ARRAY['org_admin']) THEN
            RAISE EXCEPTION 'only an org admin can grant support access' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.support_user_id = me THEN
            RAISE EXCEPTION 'you cannot grant support access to yourself' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT app_is_platform_admin(NEW.support_user_id) THEN
            RAISE EXCEPTION 'support access can only go to an active platform admin' USING ERRCODE = 'check_violation';
        END IF;
        NEW.consented_by := me;
        NEW.consented_at := now();
        NEW.starts_at := GREATEST(COALESCE(NEW.starts_at, now()), now());  -- no backdating
        RETURN NEW;
    END IF;

    -- UPDATE: only revocation.
    IF NEW.grant_id <> OLD.grant_id OR NEW.org_id <> OLD.org_id OR NEW.support_user_id <> OLD.support_user_id
       OR NEW.consented_by <> OLD.consented_by OR NEW.consented_at <> OLD.consented_at OR NEW.reason <> OLD.reason
       OR NEW.starts_at <> OLD.starts_at OR NEW.expires_at <> OLD.expires_at THEN
        RAISE EXCEPTION 'only revocation may change a support grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'support grant already revoked' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'only revocation may change a support grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT (app_is_member(OLD.org_id, me, ARRAY['org_admin']) OR OLD.support_user_id = me OR app_system_job() IS NOT NULL) THEN
        RAISE EXCEPTION 'not allowed to revoke this support grant' USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.revoked_by := me;
    NEW.revoked_at := now();
    RETURN NEW;
END $$;

CREATE FUNCTION trg_support_access_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit_write('support_access.grant', 'support_access_grant', NEW.grant_id::text, NEW.org_id,
            jsonb_build_object('support_user_id', NEW.support_user_id, 'reason', NEW.reason,
                               'starts_at', NEW.starts_at, 'expires_at', NEW.expires_at));
    ELSE
        PERFORM audit_write('support_access.revoke', 'support_access_grant', NEW.grant_id::text, NEW.org_id,
            jsonb_build_object('support_user_id', NEW.support_user_id));
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER support_access_guard BEFORE INSERT OR UPDATE ON support_access_grants
    FOR EACH ROW EXECUTE FUNCTION trg_support_access_guard();
CREATE TRIGGER support_access_audit AFTER INSERT OR UPDATE ON support_access_grants
    FOR EACH ROW EXECUTE FUNCTION trg_support_access_audit();

-- ---------------------------------------------------------------------------
-- users: changes are privileged-only (0004) and now always audited.
-- ---------------------------------------------------------------------------

CREATE FUNCTION trg_users_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit_write('user.create', 'user', NEW.user_id::text, NULL,
            jsonb_build_object('email', NEW.email, 'is_platform_admin', NEW.is_platform_admin));
    ELSE
        PERFORM audit_write('user.update', 'user', NEW.user_id::text, NULL,
            jsonb_build_object('changed', app_changed_columns(to_jsonb(OLD), to_jsonb(NEW),
                ARRAY['cognito_sub', 'email', 'display_name', 'is_platform_admin', 'disabled_at'])));
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER users_audit AFTER INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION trg_users_audit();

-- ---------------------------------------------------------------------------
-- audit_log is append-only for the API (no rights at all) and can't be
-- edited or deleted BY ACCIDENT by the privileged role (a named reason is
-- required). It is not tamper-proof against the database owner, who can
-- disable triggers; true immutability needs export to an external store.
-- ---------------------------------------------------------------------------

CREATE FUNCTION trg_audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF app_system_job() IS NULL THEN
        RAISE EXCEPTION 'audit records cannot be changed or removed' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN COALESCE(OLD, NEW);
END $$;

CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION trg_audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION trg_audit_log_immutable();

-- ---------------------------------------------------------------------------
-- Privileges for the API's runtime role (overrides 0002's defaults).
-- ---------------------------------------------------------------------------

REVOKE ALL ON organizations, org_memberships, support_access_grants, audit_log FROM app_runtime;
REVOKE ALL ON SEQUENCE audit_log_audit_id_seq FROM app_runtime;

GRANT SELECT, INSERT ON organizations TO app_runtime;
GRANT UPDATE (display_name, status) ON organizations TO app_runtime;
GRANT SELECT, INSERT ON org_memberships TO app_runtime;
GRANT UPDATE (revoked_at) ON org_memberships TO app_runtime;
GRANT SELECT, INSERT ON support_access_grants TO app_runtime;
GRANT UPDATE (revoked_at) ON support_access_grants TO app_runtime;
-- audit_log: nothing. Rows arrive only through audit_write and the triggers.

-- Functions default to EXECUTE for PUBLIC; lock them down.
REVOKE EXECUTE ON FUNCTION
    app_is_privileged_session(), app_actor(), app_current_user_id(), app_system_job(), app_require_read_committed(),
    app_user_active(uuid), app_is_platform_admin(uuid), app_is_member(uuid, uuid, text[]),
    app_active_support_grant(uuid, uuid), audit_write(text, text, text, uuid, jsonb),
    app_changed_columns(jsonb, jsonb, text[])
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_is_member(uuid, uuid, text[]), app_active_support_grant(uuid, uuid),
    audit_write(text, text, text, uuid, jsonb)
TO app_runtime;
