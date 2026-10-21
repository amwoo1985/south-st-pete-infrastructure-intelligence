#!/bin/bash
# Creates (idempotently) the Cognito user pool, hosted login domain and
# browser app client for the grant app (DECISIONS #158, #162).
#
# Settings (each verified against AWS docs, DECISIONS #162):
#   - Essentials tier: needed for managed login; free under 10,000 MAU.
#   - Admin-created accounts only (no self sign-up). Sign-in name = email.
#   - MFA REQUIRED, authenticator app (TOTP) only. Account recovery by
#     verified email (can't collide with the MFA factor: TOTP isn't email).
#   - Public app client (no secret), authorization-code grant only (the app
#     uses PKCE), scopes openid+email. No direct password API flows: sign-in
#     goes through managed login. The client reads only email/email_verified
#     and writes only email (required attributes must be writable). Token
#     revocation on, user-existence errors hidden. Access/ID tokens 60 min,
#     refresh 12 h, 15-minute sign-in session (first sign-in chains a new
#     password and TOTP enrollment; the 3-minute default is too tight).
#   - Callback/sign-out URL: http://localhost:8000/ only. Cognito accepts
#     plain http for localhost only; production needs an https address,
#     which doesn't exist yet (DECISIONS #162).
#   - Deletion protection on. Cognito's built-in email sender (low daily
#     cap, shared by invites and password resets; fine for a handful of
#     orgs, switch to SES before onboarding many people at once).
#
# Re-running: finds the existing pool/domain/client by name and reuses them,
# re-asserts MFA, and creates the managed-login branding if it's missing.
# It does NOT re-apply other pool/client settings to an existing pool; change
# those deliberately (console or update-* calls) and record it in DECISIONS.
# WARNING: UpdateUserPool resets every parameter you leave out to its default
# (AWS API reference), which would silently turn MFA off and re-open self
# sign-up. Always start from describe-user-pool, edit the full configuration,
# send all of it back, then re-run this script and confirm MFA is ON and
# AllowAdminCreateUserOnly is true.
#
# Permissions: the deploy user has no Cognito access by default. An admin
# attaches infra/cognito-iam-policy.json to it once, or runs this with an
# admin profile. The policy's pool-level actions are scoped to the one live
# pool's ARN; creating a replacement pool means updating that ARN first.
#
# Usage:
#   AWS_PROFILE=south-st-pete bash infra/register_cognito.sh
#   AWS_PROFILE=south-st-pete bash infra/register_cognito.sh invite someone@example.org
#   (optional) DOMAIN_PREFIX=my-unique-prefix to override the login domain name
# Prints the non-secret IDs to add to .env (COGNITO_*). Nothing here is a
# secret: pool ID, client ID and domain are public by design for a browser app.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
export MSYS_NO_PATHCONV=1

POOL_NAME=coalition-grant-app
CLIENT_NAME=coalition-grant-app-web
CALLBACK_URL=http://localhost:8000/

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
file_uri() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then p="$(cygpath -m "$p")"; fi
  printf 'file://%s' "$p"
}

# Pool names aren't unique in Cognito: refuse to guess between duplicates.
POOL_IDS="$(aws cognito-idp list-user-pools --max-results 60 \
  --query "UserPools[?Name=='${POOL_NAME}'].Id" --output text)"
POOL_COUNT="$(printf '%s\n' $POOL_IDS | grep -c . || true)"
if [ "$POOL_COUNT" -gt 1 ]; then
  echo "More than one user pool is named ${POOL_NAME} (${POOL_IDS}). Resolve that by hand first." >&2
  exit 1
fi
POOL_ID="${POOL_IDS}"

if [ "${1:-}" = "invite" ]; then
  EMAIL="${2:?usage: $0 invite <email>}"
  if ! printf '%s' "$EMAIL" | grep -Eq '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'; then
    echo "That doesn't look like an email address: ${EMAIL}" >&2
    exit 1
  fi
  if [ -z "$POOL_ID" ]; then
    echo "No user pool named ${POOL_NAME} yet. Run without arguments first." >&2
    exit 1
  fi
  # A typo here sends the temporary password, and every future recovery
  # email, to whoever owns the mistyped address. Make the admin look at it.
  echo "About to invite: ${EMAIL}"
  echo "Cognito will email a temporary password there (valid 7 days), and this address becomes the account's recovery address."
  read -r -p "Type the address again to confirm: " CONFIRM
  if [ "$CONFIRM" != "$EMAIL" ]; then
    echo "Addresses don't match; nothing was sent." >&2
    exit 1
  fi
  # Cognito generates the temporary password and emails it; it never passes
  # through this script or the terminal. The user starts in
  # FORCE_CHANGE_PASSWORD and must set a password and enroll TOTP.
  aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" --username "$EMAIL" \
    --user-attributes "[{\"Name\":\"email\",\"Value\":\"${EMAIL}\"},{\"Name\":\"email_verified\",\"Value\":\"true\"}]" \
    --desired-delivery-mediums EMAIL \
    --query 'User.{username:Username,status:UserStatus}' --output text
  echo "OK: invited ${EMAIL}."
  exit 0
fi

if [ -z "$POOL_ID" ]; then
  cat > "$TMP_DIR/pool.json" <<EOF
{
  "PoolName": "${POOL_NAME}",
  "UserPoolTier": "ESSENTIALS",
  "DeletionProtection": "ACTIVE",
  "UsernameAttributes": ["email"],
  "AutoVerifiedAttributes": ["email"],
  "UsernameConfiguration": {"CaseSensitive": false},
  "Schema": [{"Name": "email", "AttributeDataType": "String", "Required": true, "Mutable": true}],
  "UserAttributeUpdateSettings": {"AttributesRequireVerificationBeforeUpdate": ["email"]},
  "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": true},
  "Policies": {"PasswordPolicy": {
    "MinimumLength": 12, "RequireUppercase": true, "RequireLowercase": true,
    "RequireNumbers": true, "RequireSymbols": true, "TemporaryPasswordValidityDays": 7
  }},
  "AccountRecoverySetting": {"RecoveryMechanisms": [{"Priority": 1, "Name": "verified_email"}]},
  "EmailConfiguration": {"EmailSendingAccount": "COGNITO_DEFAULT"},
  "MfaConfiguration": "OFF"
}
EOF
  POOL_ID="$(aws cognito-idp create-user-pool --cli-input-json "$(file_uri "$TMP_DIR/pool.json")" \
    --query 'UserPool.Id' --output text)"
  echo "created user pool ${POOL_ID}"
else
  echo "reusing user pool ${POOL_ID}"
fi

# MFA required, TOTP only. Set separately because CreateUserPool can only
# enable SMS MFA; TOTP needs SetUserPoolMfaConfig. Re-asserted on every run.
echo "MFA: $(aws cognito-idp set-user-pool-mfa-config --user-pool-id "$POOL_ID" \
  --software-token-mfa-configuration Enabled=true --mfa-configuration ON \
  --query 'MfaConfiguration' --output text)"

# Hosted login domain: <prefix>.auth.us-east-1.amazoncognito.com. The prefix
# must be globally unique across AWS; override DOMAIN_PREFIX if it's taken.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DOMAIN_PREFIX="${DOMAIN_PREFIX:-coalition-grants-${ACCOUNT_ID: -6}}"
EXISTING_DOMAIN="$(aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" --query 'UserPool.Domain' --output text)"
if [ -z "$EXISTING_DOMAIN" ] || [ "$EXISTING_DOMAIN" = "None" ]; then
  if ! aws cognito-idp create-user-pool-domain --user-pool-id "$POOL_ID" --domain "$DOMAIN_PREFIX" \
      --managed-login-version 2 --query 'ManagedLoginVersion' --output text; then
    echo "Could not create login domain '${DOMAIN_PREFIX}' (it may be taken). Re-run with DOMAIN_PREFIX=<another-unique-name>." >&2
    exit 1
  fi
  EXISTING_DOMAIN="$DOMAIN_PREFIX"
fi

CLIENT_IDS="$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 60 \
  --query "UserPoolClients[?ClientName=='${CLIENT_NAME}'].ClientId" --output text)"
if [ "$(printf '%s\n' $CLIENT_IDS | grep -c . || true)" -gt 1 ]; then
  echo "More than one app client is named ${CLIENT_NAME} (${CLIENT_IDS}). Resolve that by hand first." >&2
  exit 1
fi
CLIENT_ID="${CLIENT_IDS}"
if [ -z "$CLIENT_ID" ]; then
  cat > "$TMP_DIR/client.json" <<EOF
{
  "UserPoolId": "${POOL_ID}",
  "ClientName": "${CLIENT_NAME}",
  "GenerateSecret": false,
  "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
  "SupportedIdentityProviders": ["COGNITO"],
  "AllowedOAuthFlowsUserPoolClient": true,
  "AllowedOAuthFlows": ["code"],
  "AllowedOAuthScopes": ["openid", "email"],
  "CallbackURLs": ["${CALLBACK_URL}"],
  "LogoutURLs": ["${CALLBACK_URL}"],
  "ReadAttributes": ["email", "email_verified"],
  "WriteAttributes": ["email"],
  "PreventUserExistenceErrors": "ENABLED",
  "EnableTokenRevocation": true,
  "AuthSessionValidity": 15,
  "AccessTokenValidity": 60,
  "IdTokenValidity": 60,
  "RefreshTokenValidity": 12,
  "TokenValidityUnits": {"AccessToken": "minutes", "IdToken": "minutes", "RefreshToken": "hours"}
}
EOF
  CLIENT_ID="$(aws cognito-idp create-user-pool-client --cli-input-json "$(file_uri "$TMP_DIR/client.json")" \
    --query 'UserPoolClient.ClientId' --output text)"
  echo "created app client ${CLIENT_ID}"
else
  echo "reusing app client ${CLIENT_ID}"
fi

# Managed login (v2) pages need a branding style per client. Checked on every
# run so a failure on a previous run is repaired, not silently skipped. Only
# "not found" means missing; any other error stops the script.
if BRANDING_ERR="$(aws cognito-idp describe-managed-login-branding-by-client \
    --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
    --query 'ManagedLoginBranding.ManagedLoginBrandingId' --output text 2>&1)"; then
  echo "managed login branding present"
elif printf '%s' "$BRANDING_ERR" | grep -q "ResourceNotFoundException"; then
  aws cognito-idp create-managed-login-branding --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
    --use-cognito-provided-values --query 'ManagedLoginBranding.ManagedLoginBrandingId' --output text
  echo "created managed login branding"
else
  echo "Could not check managed login branding: ${BRANDING_ERR}" >&2
  exit 1
fi

echo
echo "OK. Add these (non-secret) lines to .env:"
echo "COGNITO_REGION=us-east-1"
echo "COGNITO_USER_POOL_ID=${POOL_ID}"
echo "COGNITO_APP_CLIENT_ID=${CLIENT_ID}"
echo "COGNITO_DOMAIN=${EXISTING_DOMAIN}.auth.us-east-1.amazoncognito.com"
