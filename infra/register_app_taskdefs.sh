#!/bin/bash
# Fills infra/{api,worker}-taskdef.template.json placeholders from the
# repo-root .env (never committed — DECISIONS #69) and registers both
# task definitions for real. Templates hold ONLY placeholders
# (__PLACEHOLDER__ tokens), never real values — .claude/rules/deploy.md's
# "secrets never live in committed config" rule (DECISIONS #119/#120:
# plain environment variables are this project's chosen secrets path,
# since neither Secrets Manager nor SSM Parameter Store access exists on
# the deploy IAM user; the real-value task definition still ends up
# holding a plaintext secret once registered — see #120's disclosure).
#
# Usage:
#   IMAGE_TAG=<git-sha> infra/register_app_taskdefs.sh
# Requires: AWS_PROFILE set, .env present with RDS_MASTER_USERNAME/
# RDS_MASTER_PASSWORD/OPENAI_API_KEY, docker image already pushed to ECR
# under the given tag.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE_TAG:?Set IMAGE_TAG to the git commit SHA of the pushed image (e.g. IMAGE_TAG=$(git rev-parse --short HEAD))}"
: "${AWS_PROFILE:?Set AWS_PROFILE}"

set -a
source .env
set +a
: "${RDS_HOST:?Set RDS_HOST in .env (see .env.example) — the real RDS endpoint}"

export AWS_DEFAULT_REGION=us-east-1
export MSYS_NO_PATHCONV=1

# Derived live, not hardcoded (deploy-review pre-commit finding, DECISIONS
# #121) — this script stays correct if it's ever run under a different
# AWS account/profile rather than silently pushing to the wrong registry.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app:${IMAGE_TAG}"
DB_HOST="${RDS_HOST}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

for role in api worker; do
  OUT_FILE="${TMP_DIR}/${role}-taskdef.json"
  sed \
    -e "s#__IMAGE__#${IMAGE}#g" \
    -e "s#__DB_HOST__#${DB_HOST}#g" \
    -e "s#__RDS_MASTER_USERNAME__#${RDS_MASTER_USERNAME}#g" \
    -e "s#__RDS_MASTER_PASSWORD__#${RDS_MASTER_PASSWORD}#g" \
    -e "s#__OPENAI_API_KEY__#${OPENAI_API_KEY}#g" \
    "infra/${role}-taskdef.template.json" > "${OUT_FILE}"
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
