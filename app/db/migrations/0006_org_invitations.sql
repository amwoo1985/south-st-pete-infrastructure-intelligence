-- Invitations (DECISIONS #170, #171): joining an organization is a two-step
-- act. An org admin or point person creates a pending invitation; the person
-- gets a role only when they accept it. Nothing is granted by inviting.
--
-- Trust model, stated plainly:
-- * Direct inserts into org_memberships are no longer possible for the API's
--   runtime role. The only ways a membership appears are (1) the invitee
--   accepting a pending invitation through accept_invitation(), and (2) the
--   privileged session (a login role in app_privileged) with a named
--   app.system_job reason, used by the bootstrap/recovery command.
-- * The invitation functions below are SECURITY DEFINER and are the only
--   writers of org_invitations (app_runtime has SELECT on it, nothing else).
-- * Acceptance works by setting the transaction-local setting
--   app.accepting_invitation to the invitation's id. The membership rule
--   honors it only when it matches a pending invitation for exactly that
--   (org, person, role) addressed to the acting person, so forging the
--   setting buys nothing beyond what the invitation already allows.
-- * Stated limit: until row-level security arrives, any signed-in user could
--   read org_invitations directly at the database level; the API exposes only
--   a person's own invitations and an org admin's list for their org.
--
-- SQLSTATEs the API maps: P0A01 account_needed, P0A02 invitation cap,
-- P0A03 no such invitation (unknown, not yours, used, expired, not allowed).

CREATE TABLE org_invitations (
    invitation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations (org_id),
    invited_user_id UUID NOT NULL REFERENCES users (user_id),
    role TEXT NOT NULL CHECK (role IN ('org_admin', 'point_person', 'contributor', 'budget_owner', 'authorized_approver')),
    invited_by UUID NOT NULL REFERENCES users (user_id),
    invited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '14 days',
    accepted_at TIMESTAMPTZ,
    declined_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoked_by UUID REFERENCES users (user_id),
    CHECK (invited_by <> invited_user_id),
    CHECK (expires_at > invited_at AND expires_at <= invited_at + interval '14 days'),
    CHECK ((accepted_at IS NOT NULL)::int + (declined_at IS NOT NULL)::int + (revoked_at IS NOT NULL)::int <= 1),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
-- At most one open invitation per (org, person, role). An expired one that
-- was never closed still occupies this slot; invite_user() renews it.
CREATE UNIQUE INDEX org_invitations_one_open
    ON org_invitations (org_id, invited_user_id, role)
    WHERE accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL;
CREATE INDEX org_invitations_invitee ON org_invitations (invited_user_id)
    WHERE accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL;
CREATE INDEX org_invitations_org_at ON org_invitations (org_id, invited_at);
CREATE INDEX org_invitations_sender_at ON org_invitations (invited_by, invited_at);
CREATE INDEX org_invitations_at ON org_invitations (invited_at);

-- Only status changes and renewal of an expired open invitation are allowed.
CREATE FUNCTION trg_org_invitations_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.invitation_id <> OLD.invitation_id OR NEW.org_id <> OLD.org_id
       OR NEW.invited_user_id <> OLD.invited_user_id OR NEW.role <> OLD.role THEN
        RAISE EXCEPTION 'only the status of an invitation may change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.accepted_at IS NOT NULL OR OLD.declined_at IS NOT NULL OR OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'that invitation is closed' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.invited_at <> OLD.invited_at THEN  -- renewal
        IF OLD.expires_at > now() OR NEW.accepted_at IS NOT NULL OR NEW.declined_at IS NOT NULL OR NEW.revoked_at IS NOT NULL THEN
            RAISE EXCEPTION 'only an expired open invitation can be renewed' USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.invited_by <> OLD.invited_by OR NEW.expires_at <> OLD.expires_at THEN
        RAISE EXCEPTION 'only the status of an invitation may change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER org_invitations_guard BEFORE UPDATE ON org_invitations
    FOR EACH ROW EXECUTE FUNCTION trg_org_invitations_guard();

-- Database-written audit for invitations (the 'invitation' area is reserved
-- below, so the API cannot forge these).
CREATE FUNCTION trg_org_invitations_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit_write('invitation.create', 'invitation', NEW.invitation_id::text, NEW.org_id,
            jsonb_build_object('target_user_id', NEW.invited_user_id, 'role', NEW.role));
    ELSIF NEW.invited_at <> OLD.invited_at THEN
        PERFORM audit_write('invitation.create', 'invitation', NEW.invitation_id::text, NEW.org_id,
            jsonb_build_object('target_user_id', NEW.invited_user_id, 'role', NEW.role, 'renewed', true));
    ELSIF NEW.accepted_at IS NOT NULL THEN
        PERFORM audit_write('invitation.accept', 'invitation', NEW.invitation_id::text, NEW.org_id,
            jsonb_build_object('role', NEW.role));
    ELSIF NEW.declined_at IS NOT NULL THEN
        PERFORM audit_write('invitation.decline', 'invitation', NEW.invitation_id::text, NEW.org_id,
            jsonb_build_object('role', NEW.role));
    ELSIF NEW.revoked_at IS NOT NULL THEN
        PERFORM audit_write('invitation.revoke', 'invitation', NEW.invitation_id::text, NEW.org_id,
            jsonb_build_object('role', NEW.role, 'by_sender', NEW.revoked_by = NEW.invited_by));
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER org_invitations_audit AFTER INSERT OR UPDATE ON org_invitations
    FOR EACH ROW EXECUTE FUNCTION trg_org_invitations_audit();

-- audit_write: same as before, plus the 'invitation' area is reserved for
-- the database.
CREATE OR REPLACE FUNCTION audit_write(p_action text, p_object_type text, p_object_id text, p_org uuid, p_detail jsonb)
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
            IF p_action !~ '^[a-z_]+\.[a-z_]+$' THEN
                RAISE EXCEPTION 'audit action % must look like "area.event" in lowercase', p_action
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF split_part(p_action, '.', 1) IN ('org', 'membership', 'support_access', 'user', 'invitation') THEN
                RAISE EXCEPTION 'audit action % is reserved for the database', p_action USING ERRCODE = 'insufficient_privilege';
            END IF;
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

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

-- Whether `p_by` may, right now, grant `p_role` in `p_org` (active admin for
-- org_admin; active admin or point person for the other roles). The same
-- question is asked when inviting and again when accepting.
CREATE FUNCTION app_can_invite(p_org uuid, p_by uuid, p_role text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT app_user_active(p_by) AND app_is_member(p_org, p_by,
        CASE WHEN p_role = 'org_admin' THEN ARRAY['org_admin'] ELSE ARRAY['org_admin', 'point_person'] END)
$$;

-- Authorization, then caps: the part of inviting that depends only on the
-- caller. Authorization comes first, with one refusal for "no such org" and
-- "not yours"; the caps come next and before anything about the target. Caps
-- are over a rolling 24 hours and every kind of invitation counts, closed ones
-- included.
CREATE FUNCTION app_invite_gate(p_me uuid, p_org uuid, p_role text) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_org_cap constant int := 20;
    v_caller_cap constant int := 10;
    v_global_cap constant int := 30;
BEGIN
    IF NOT app_user_active(p_me) OR app_is_platform_admin(p_me) THEN
        RAISE EXCEPTION 'not allowed to invite people to this organization' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_role IS NULL OR p_role NOT IN ('org_admin', 'point_person', 'contributor', 'budget_owner', 'authorized_approver') THEN
        RAISE EXCEPTION 'unknown role' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF NOT app_can_invite(p_org, p_me, p_role) THEN
        RAISE EXCEPTION 'not allowed to invite people to this organization' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (SELECT count(*) FROM org_invitations i WHERE i.org_id = p_org AND i.invited_at > now() - interval '24 hours') >= v_org_cap THEN
        RAISE EXCEPTION 'too many invitations today for this organization' USING ERRCODE = 'P0A02';
    END IF;
    IF (SELECT count(*) FROM org_invitations i WHERE i.invited_by = p_me AND i.invited_at > now() - interval '24 hours') >= v_caller_cap THEN
        RAISE EXCEPTION 'you have sent too many invitations today' USING ERRCODE = 'P0A02';
    END IF;
    IF (SELECT count(*) FROM org_invitations i WHERE i.invited_at > now() - interval '24 hours') >= v_global_cap THEN
        -- Stated limits: this reveals to any authorized inviter that 30 invitations
        -- went out across all tenants in 24 hours, and the global count is not
        -- serialized, so concurrent inviters can overshoot it slightly.
        RAISE EXCEPTION 'invitations are limited right now; try again later' USING ERRCODE = 'P0A02';
    END IF;
END $$;

-- Step one of inviting from the API: confirm the caller may invite and is
-- under the caps, THEN take the per-sender and per-organization locks, and
-- check again under them. The API keeps the transaction open (the locks last
-- until it ends) while it talks to Cognito and then calls invite_user, so
-- concurrent requests can't all pass the caps and each create a Cognito
-- account. Authorization comes before any lock, so a stranger can't queue
-- behind or time another tenant's organization row.
CREATE FUNCTION invite_begin(p_org uuid, p_role text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
BEGIN
    PERFORM app_require_read_committed();
    PERFORM app_invite_gate(me, p_org, p_role);
    -- Waiting for another invitation (or anything else holding this org) is
    -- bounded: the request fails with lock_not_available instead of queueing.
    PERFORM set_config('lock_timeout', '3000', true);
    PERFORM pg_advisory_xact_lock(hashtext('invite:' || me::text));
    PERFORM 1 FROM organizations WHERE org_id = p_org FOR UPDATE;
    PERFORM app_invite_gate(me, p_org, p_role);
END $$;

-- The pending-invitation id named by app.accepting_invitation, or NULL.
CREATE FUNCTION app_accepting_invitation() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE v text := NULLIF(current_setting('app.accepting_invitation', true), '');
BEGIN
    IF v IS NULL OR v !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RETURN NULL;
    END IF;
    RETURN v::uuid;
END $$;

-- ---------------------------------------------------------------------------
-- invite_user: create (or renew) a pending invitation. Grants nothing.
--
-- Same answer whether the person is new, existing, already a member,
-- disabled or vendor staff: a row is created in every case (the unactionable
-- ones simply can never be accepted), and every case counts toward the caps.
-- The one exception is P0A01 (account_needed) for an unknown address when no
-- account id was supplied: it writes nothing, so it is a free existence probe
-- for anyone able to call this function directly. The API never relies on
-- it: it always looks up or creates the Cognito account first (after
-- invite_begin) and passes the account id.
-- Returns the invitation, the normalized email, and whether this call
-- created or renewed it (the API then re-sends the Cognito invitation if the
-- person has never signed in).
-- ---------------------------------------------------------------------------

CREATE FUNCTION invite_user(p_org uuid, p_email text, p_role text, p_display_name text, p_cognito_sub text)
RETURNS TABLE (out_invitation_id uuid, out_email text, out_created boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_email text;
    v_target uuid;
    v_open org_invitations%ROWTYPE;
    v_new uuid;
BEGIN
    PERFORM app_require_read_committed();
    -- Authorize before taking any lock, so a stranger can't queue behind (or
    -- time) another tenant's org row.
    PERFORM app_invite_gate(me, p_org, p_role);
    -- One admin in several orgs can't overshoot the per-caller cap concurrently.
    PERFORM pg_advisory_xact_lock(hashtext('invite:' || me::text));
    PERFORM 1 FROM organizations WHERE org_id = p_org FOR UPDATE;

    -- Re-check under the locks (authorization, then the caps, before the target).
    PERFORM app_invite_gate(me, p_org, p_role);

    -- Input checks (ASCII-only email so Postgres, Python and Cognito agree on case).
    v_email := lower(btrim(COALESCE(p_email, '')));
    IF length(v_email) > 254 OR v_email !~ '^[a-z0-9.!#$%&''*+/=?^_`{|}~-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$' THEN
        RAISE EXCEPTION 'that is not a valid email address' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF p_display_name IS NOT NULL AND (length(p_display_name) > 200 OR p_display_name ~ '[[:cntrl:]]') THEN
        RAISE EXCEPTION 'that name is not acceptable' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF v_email = (SELECT u.email FROM users u WHERE u.user_id = me) THEN
        RAISE EXCEPTION 'you cannot invite yourself' USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT u.user_id INTO v_target FROM users u WHERE u.email = v_email;
    IF v_target IS NULL THEN
        IF p_cognito_sub IS NULL OR btrim(p_cognito_sub) = '' THEN
            RAISE EXCEPTION 'account_needed' USING ERRCODE = 'P0A01';
        END IF;
        IF p_cognito_sub !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'invalid account id' USING ERRCODE = 'invalid_parameter_value';
        END IF;
        INSERT INTO users (cognito_sub, email, display_name, is_platform_admin)
        VALUES (lower(p_cognito_sub), v_email, NULLIF(btrim(p_display_name), ''), false)
        ON CONFLICT DO NOTHING;
        SELECT u.user_id INTO v_target FROM users u WHERE u.email = v_email AND u.cognito_sub = lower(p_cognito_sub);
        IF v_target IS NULL THEN
            RAISE EXCEPTION 'that account could not be set up' USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    SELECT * INTO v_open FROM org_invitations i
    WHERE i.org_id = p_org AND i.invited_user_id = v_target AND i.role = p_role
      AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL
    FOR UPDATE;
    IF FOUND THEN
        IF v_open.expires_at > now() THEN
            RETURN QUERY SELECT v_open.invitation_id, v_email, false;
            RETURN;
        END IF;
        UPDATE org_invitations i SET invited_by = me, invited_at = now(), expires_at = now() + interval '14 days'
        WHERE i.invitation_id = v_open.invitation_id;
        RETURN QUERY SELECT v_open.invitation_id, v_email, true;
        RETURN;
    END IF;

    INSERT INTO org_invitations (org_id, invited_user_id, role, invited_by)
    VALUES (p_org, v_target, p_role, me) RETURNING org_invitations.invitation_id INTO v_new;
    RETURN QUERY SELECT v_new, v_email, true;
END $$;

-- ---------------------------------------------------------------------------
-- accept / decline / revoke. Anything that can't proceed is "no such
-- invitation" (P0A03), whatever the reason, so the answer reveals nothing.
-- ---------------------------------------------------------------------------

CREATE FUNCTION accept_invitation(p_invitation uuid) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_inv org_invitations%ROWTYPE;
    v_status text;
    v_org uuid;
    v_membership uuid;
BEGIN
    PERFORM app_require_read_committed();
    -- Lock order everywhere: organization first, then the invitation.
    SELECT i.org_id INTO v_org FROM org_invitations i
    WHERE i.invitation_id = p_invitation AND i.invited_user_id = me
      AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such invitation' USING ERRCODE = 'P0A03';
    END IF;
    SELECT status INTO v_status FROM organizations WHERE org_id = v_org FOR UPDATE;
    SELECT * INTO v_inv FROM org_invitations i WHERE i.invitation_id = p_invitation AND i.invited_user_id = me FOR UPDATE;
    IF v_inv.accepted_at IS NOT NULL OR v_inv.declined_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL
       OR v_inv.expires_at <= now()
       OR v_status IS DISTINCT FROM 'active'
       OR NOT app_user_active(me) OR app_is_platform_admin(me)
       OR NOT app_can_invite(v_inv.org_id, v_inv.invited_by, v_inv.role) THEN
        RAISE EXCEPTION 'no such invitation' USING ERRCODE = 'P0A03';
    END IF;
    PERFORM set_config('app.accepting_invitation', p_invitation::text, true);
    BEGIN
        INSERT INTO org_memberships (org_id, user_id, role) VALUES (v_inv.org_id, me, v_inv.role)
        RETURNING membership_id INTO v_membership;
    EXCEPTION WHEN unique_violation THEN
        PERFORM set_config('app.accepting_invitation', '', true);
        RAISE EXCEPTION 'no such invitation' USING ERRCODE = 'P0A03';
    END;
    PERFORM set_config('app.accepting_invitation', '', true);
    RETURN v_membership;
END $$;

CREATE FUNCTION decline_invitation(p_invitation uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_inv org_invitations%ROWTYPE;
BEGIN
    PERFORM app_require_read_committed();
    SELECT * INTO v_inv FROM org_invitations i WHERE i.invitation_id = p_invitation AND i.invited_user_id = me FOR UPDATE;
    IF NOT FOUND OR v_inv.accepted_at IS NOT NULL OR v_inv.declined_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL
       OR v_inv.expires_at <= now() OR NOT app_user_active(me) THEN
        RAISE EXCEPTION 'no such invitation' USING ERRCODE = 'P0A03';
    END IF;
    UPDATE org_invitations i SET declined_at = now() WHERE i.invitation_id = p_invitation;
END $$;

CREATE FUNCTION revoke_invitation(p_invitation uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_inv org_invitations%ROWTYPE;
BEGIN
    PERFORM app_require_read_committed();
    SELECT * INTO v_inv FROM org_invitations i WHERE i.invitation_id = p_invitation FOR UPDATE;
    IF NOT FOUND OR v_inv.accepted_at IS NOT NULL OR v_inv.declined_at IS NOT NULL OR v_inv.revoked_at IS NOT NULL
       OR NOT (app_is_member(v_inv.org_id, me, ARRAY['org_admin'])
               OR (v_inv.invited_by = me AND app_is_member(v_inv.org_id, me, ARRAY['org_admin', 'point_person']))) THEN
        RAISE EXCEPTION 'no such invitation' USING ERRCODE = 'P0A03';
    END IF;
    UPDATE org_invitations i SET revoked_at = now(), revoked_by = me WHERE i.invitation_id = p_invitation;
END $$;

-- ---------------------------------------------------------------------------
-- Membership rule: direct grants are gone. The two ways in are acceptance of
-- a matching pending invitation and the privileged, reasoned path.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_org_memberships_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    me uuid := app_current_user_id();
    v_status text;
    v_privileged boolean := app_system_job() IS NOT NULL;
    v_other_admins int;
    v_flag uuid := app_accepting_invitation();
    v_inv org_invitations%ROWTYPE;
BEGIN
    PERFORM app_require_read_committed();
    -- Serialize all membership changes for this org (no grant/revoke races).
    SELECT status INTO v_status FROM organizations WHERE org_id = COALESCE(NEW.org_id, OLD.org_id) FOR UPDATE;

    IF TG_OP = 'INSERT' THEN
        NEW.granted_at := now();
        IF NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL THEN
            RAISE EXCEPTION 'a new membership cannot start revoked' USING ERRCODE = 'check_violation';
        END IF;

        -- Path 1: the invitee accepting a matching, pending invitation.
        IF v_flag IS NOT NULL THEN
            SELECT * INTO v_inv FROM org_invitations i
            WHERE i.invitation_id = v_flag AND i.org_id = NEW.org_id AND i.invited_user_id = NEW.user_id
              AND i.role = NEW.role AND NEW.user_id = me
              AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
            IF FOUND THEN
                IF v_status IS DISTINCT FROM 'active' OR NOT app_user_active(me) OR app_is_platform_admin(me)
                   OR NOT app_can_invite(NEW.org_id, v_inv.invited_by, NEW.role) THEN
                    RAISE EXCEPTION 'that invitation can no longer be accepted' USING ERRCODE = 'insufficient_privilege';
                END IF;
                NEW.granted_by := v_inv.invited_by;
                RETURN NEW;
            END IF;
            -- A setting that matches no pending invitation is ignored.
        END IF;

        -- Path 2: recovery/bootstrap by the privileged session, audited with its reason.
        IF v_privileged THEN
            NEW.granted_by := me;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'memberships are created by accepting an invitation' USING ERRCODE = 'insufficient_privilege';
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

-- After a membership row is inserted through acceptance (recognizable
-- because its granted_by is the inviter, never the acting person), close the
-- matching invitation in the same statement; if none can be closed the whole
-- insert fails. It keys on the row, not on the setting, so a multi-row insert
-- or a changed setting can't skip it. Privileged inserts with a named reason
-- are not tied to an invitation. Named so it runs before
-- org_memberships_audit.
CREATE FUNCTION trg_org_memberships_accept() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_closed int;
BEGIN
    UPDATE org_invitations i SET accepted_at = now()
    WHERE i.org_id = NEW.org_id AND i.invited_user_id = NEW.user_id AND i.role = NEW.role AND i.invited_by = NEW.granted_by
      AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now();
    GET DIAGNOSTICS v_closed = ROW_COUNT;
    -- Nothing to close is allowed only for the privileged, reasoned path
    -- (decided by session_user and the privileged role, which the runtime
    -- role cannot forge; the acting-user setting could be changed mid-statement).
    IF v_closed = 0 AND app_system_job() IS NULL THEN
        RAISE EXCEPTION 'that invitation can no longer be accepted' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER org_memberships_accept AFTER INSERT ON org_memberships
    FOR EACH ROW EXECUTE FUNCTION trg_org_memberships_accept();

-- membership.grant also records which invitation it came from, if any.
CREATE OR REPLACE FUNCTION trg_org_memberships_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_invitation uuid;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT i.invitation_id INTO v_invitation FROM org_invitations i
        WHERE i.org_id = NEW.org_id AND i.invited_user_id = NEW.user_id AND i.role = NEW.role
          AND i.accepted_at = now() AND i.invited_by = NEW.granted_by
        ORDER BY i.invited_at DESC LIMIT 1;
        PERFORM audit_write('membership.grant', 'membership', NEW.membership_id::text, NEW.org_id,
            jsonb_build_object('target_user_id', NEW.user_id, 'role', NEW.role)
            || CASE WHEN v_invitation IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('invitation_id', v_invitation) END);
    ELSE
        PERFORM audit_write('membership.revoke', 'membership', NEW.membership_id::text, NEW.org_id,
            jsonb_build_object('target_user_id', NEW.user_id, 'role', NEW.role,
                               'self_revoked', NEW.revoked_by = NEW.user_id));
    END IF;
    RETURN NULL;
END $$;

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------

REVOKE ALL ON org_invitations FROM app_runtime;
GRANT SELECT ON org_invitations TO app_runtime;

REVOKE EXECUTE ON FUNCTION
    trg_org_invitations_guard(), trg_org_invitations_audit(), trg_org_memberships_accept(),
    app_can_invite(uuid, uuid, text), app_accepting_invitation(), app_invite_gate(uuid, uuid, text),
    invite_begin(uuid, text), invite_user(uuid, text, text, text, text), accept_invitation(uuid),
    decline_invitation(uuid), revoke_invitation(uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    invite_begin(uuid, text), invite_user(uuid, text, text, text, text), accept_invitation(uuid),
    decline_invitation(uuid), revoke_invitation(uuid)
TO app_runtime;
