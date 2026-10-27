#!/bin/bash
# Fills infra/{api,migrate}-taskdef.template.json placeholders from the
# repo-root .env (never committed — DECISIONS #69) and registers both
# task definitions for real. The api runs as the limited app_runtime DB
# role; only the one-off migrate task gets the RDS master credentials
# (DECISIONS #160). Templates hold ONLY placeholders
# (__PLACEHOLDER__ tokens), never real values — .claude/rules/deploy.md's
# "secrets never live in committed config" rule (DECISIONS #119/#120:
# plain environment variables are this project's chosen secrets path,
# since neither Secrets Manager nor SSM Parameter Store access exists on
# the deploy IAM user; the real-value task definition still ends up
# holding a plaintext secret once registered — see #120's disclosure).
#
# Usage:
#   IMAGE_TAG=<git-sha> infra/register_app_taskdefs.sh
# Requires: AWS_PROFILE set, python with python-dotenv (the repo .venv),
# .env present with RDS_HOST/RDS_MASTER_USERNAME/RDS_MASTER_PASSWORD/
# APP_RUNTIME_DB_PASSWORD/OPENAI_API_KEY, plus COGNITO_REGION/COGNITO_USER_POOL_ID/
# COGNITO_APP_CLIENT_ID (public identifiers, not secrets; infra/register_cognito.sh
# prints them), CLOUDFLARE_TUNNEL_SECRET_ARN (unless TUNNEL_SIDECAR=off), and
# the docker image already pushed to ECR under the given tag. The cloudflared
# image is the digest in infra/CLOUDFLARED_PIN, mirrored into ECR beforehand by
# infra/mirror_cloudflared.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE_TAG:?Set IMAGE_TAG to the git commit SHA of the pushed image (e.g. IMAGE_TAG=$(git rev-parse --short HEAD))}"
: "${AWS_PROFILE:?Set AWS_PROFILE}"

export AWS_DEFAULT_REGION=us-east-1
export MSYS_NO_PATHCONV=1

# Derived live, not hardcoded (deploy-review pre-commit finding, DECISIONS
# #121) — this script stays correct if it's ever run under a different
# AWS account/profile rather than silently pushing to the wrong registry.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app:${IMAGE_TAG}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# tunnel-check is the pre-migration rehearsal task (cloudflared only, DECISIONS
# #178); it is skipped when the tunnel is deliberately off.
# Same rule as fill_taskdef.py: only exactly "on"/"off" (or unset) is accepted.
ROLES="api migrate"
case "${TUNNEL_SIDECAR-on}" in
  on) ROLES="api migrate tunnel-check" ;;
  off) ;;
  *) echo "TUNNEL_SIDECAR must be 'on' or 'off' (or unset for on)" >&2; exit 1 ;;
esac

# The pinned cloudflared image must already be in ECR (infra/mirror_cloudflared.sh):
# otherwise the definition registers fine and the task fails at start, and
# because cloudflared is essential that takes the whole api task with it.
if [ "${TUNNEL_SIDECAR-on}" = "on" ]; then
  PIN_DIGEST="$(sed -n 's/^CLOUDFLARED_DIGEST=//p' infra/CLOUDFLARED_PIN | tr -d '\r')"
  printf '%s' "$PIN_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' \
    || { echo "infra/CLOUDFLARED_PIN: bad CLOUDFLARED_DIGEST" >&2; exit 1; }
  if ! ECR_ERR="$(aws ecr describe-images --repository-name south-st-pete-app \
      --image-ids "imageDigest=${PIN_DIGEST}" --query 'imageDetails[0].imageDigest' \
      --output text 2>&1 >/dev/null)"; then
    echo "cloudflared ${PIN_DIGEST} not confirmed in ECR (run: bash infra/mirror_cloudflared.sh): ${ECR_ERR}" >&2
    exit 1
  fi
fi

for role in ${ROLES}; do
  OUT_FILE="${TMP_DIR}/${role}-taskdef.json"
  # All filling and checking lives in infra/fill_taskdef.py (tested in
  # tests/infra). .env is parsed by python-dotenv (quoting, CRLF), placeholders
  # are replaced as literal JSON string values, never via sed, so a secret
  # containing & # \ " or a CR can't break the JSON. Secrets travel via the
  # .env file only, never argv. The script refuses before registering anything if a
  # required value is missing or a .env.example placeholder, and refuses if
  # .env still holds CLOUDFLARE_TUNNEL_TOKEN (the token lives only in Secrets
  # Manager; the ARN, which is not secret, is CLOUDFLARE_TUNNEL_SECRET_ARN).
  python infra/fill_taskdef.py "${role}" "${IMAGE}" "${ACCOUNT_ID}" > "${OUT_FILE}"
done

# Fill everything first, register second: a refusal for any role stops the
# run before any aws register call, so nothing is half-registered.
for role in ${ROLES}; do
  OUT_FILE="${TMP_DIR}/${role}-taskdef.json"
  # aws.exe (Windows-native, e.g. Git Bash on Windows) can't resolve a POSIX
  # /tmp/... path in a --cli-input-json file:// URI. cygpath -m (drive letter
  # + forward slashes, e.g. C:/Users/...) converts it into a well-formed
  # file:// URI; -w's backslashes would not be. On non-Windows shells cygpath
  # doesn't exist, so fall back to the plain path.
  if command -v cygpath >/dev/null 2>&1; then
    OUT_FILE="$(cygpath -m "${OUT_FILE}")"
  fi
  aws ecs register-task-definition --cli-input-json "file://${OUT_FILE}" \
    --query 'taskDefinition.taskDefinitionArn' --output text
done
