-- People who can sign in. Identity (password, MFA) lives in Cognito; this
-- table is the app's own record of each person, keyed by the Cognito user's
-- immutable `sub`. A request is only served for a row that exists and isn't
-- disabled, so disabling someone here takes effect on their next request,
-- even while their Cognito token is still unexpired.

CREATE TABLE users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cognito_sub TEXT NOT NULL UNIQUE,
    -- Stored lowercase and trimmed; Cognito treats sign-in emails
    -- case-insensitively, so one person can't end up with two rows.
    email TEXT NOT NULL CHECK (email = lower(btrim(email)) AND email <> ''),
    display_name TEXT,
    -- Can administer the platform (create orgs, grant org admins). Does NOT
    -- by itself grant access to any organization's documents (questionnaire
    -- 2.2, 8.1); content access comes only from memberships/assignments.
    is_platform_admin BOOLEAN NOT NULL DEFAULT false,
    disabled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX users_email_key ON users (email);

-- The API's runtime role may only READ users. Creating people, promoting
-- platform admins and disabling accounts go through a privileged path (the
-- migration/admin role), so no bug in an API route can escalate a user or
-- re-enable a disabled one. Overrides 0002's default privileges for this
-- table.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON users FROM app_runtime;
GRANT SELECT ON users TO app_runtime;
