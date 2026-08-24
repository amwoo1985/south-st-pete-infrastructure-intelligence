#!/bin/bash
# Fills infra/migrate-taskdef.template.json placeholders from the
# repo-root .env and registers the migrate task definition for real.
# Mirrors infra/register_app_taskdefs.sh's pattern exactly (same secrets
# source, same cygpath fix for aws.exe on Windows/Git-Bash) — kept as a
# separate script rather than folded into register_app_taskdefs.sh
# because this registers one role (south-st-pete-migrate), not the
# api/worker pair, and is run on-demand rather than per-deploy.
#
# One registered revision is reused across many `run-task` invocations
# (see infra/run_migration_task.sh) via ECS containerOverrides — you do
# not need to re-run this script per migration command, only when the
# image tag changes.
#
# Usage:
#   IMAGE_TAG=<git-sha> infra/register_migrate_taskdef.sh
# Requires: AWS_PROFILE set, .env present with RDS_MASTER_USERNAME/
# RDS_MASTER_PASSWORD/OPENAI_API_KEY/RDS_HOST, the migrate image already
# pushed to ECR under the given tag (infra/build_push_migrate_image.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE_TAG:?Set IMAGE_TAG to the git commit SHA of the pushed migrate image}"
: "${AWS_PROFILE:?Set AWS_PROFILE}"

set -a
source .env
set +a
: "${RDS_HOST:?Set RDS_HOST in .env (see .env.example)}"

export AWS_DEFAULT_REGION=us-east-1

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app-migrate:${IMAGE_TAG}"
DB_HOST="${RDS_HOST}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

OUT_FILE="${TMP_DIR}/migrate-taskdef.json"
sed \
  -e "s#__IMAGE__#${IMAGE}#g" \
  -e "s#__DB_HOST__#${DB_HOST}#g" \
  -e "s#__RDS_MASTER_USERNAME__#${RDS_MASTER_USERNAME}#g" \
  -e "s#__RDS_MASTER_PASSWORD__#${RDS_MASTER_PASSWORD}#g" \
  -e "s#__OPENAI_API_KEY__#${OPENAI_API_KEY}#g" \
  "infra/migrate-taskdef.template.json" > "${OUT_FILE}"

# aws.exe (Windows-native, e.g. Git Bash on Windows) can't resolve a POSIX
# /tmp/... path in a --cli-input-json file:// URI; cygpath -m (drive
# letter + forward slashes) converts it into a well-formed file:// URI —
# see infra/register_app_taskdefs.sh's DECISIONS #122/#123 history for
# why -m and not -w. On non-Windows shells cygpath doesn't exist, so fall
# back to the plain path.
if command -v cygpath >/dev/null 2>&1; then
  OUT_FILE="$(cygpath -m "${OUT_FILE}")"
fi

aws ecs register-task-definition --cli-input-json "file://${OUT_FILE}" \
  --query 'taskDefinition.taskDefinitionArn' --output text
