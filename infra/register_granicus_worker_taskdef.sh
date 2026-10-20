#!/bin/bash
# Fills infra/granicus-worker-taskdef.template.json placeholders from the
# repo-root .env and registers the south-st-pete-granicus-worker task
# definition for real. Mirrors infra/register_migrate_taskdef.sh's
# pattern exactly (same secrets source, same cygpath fix for aws.exe on
# Windows/Git-Bash) — a SEPARATE task family from both
# south-st-pete-worker (the always-on service this replaces, scaled to
# desiredCount=0 once this is live-verified, not deregistered — matches
# DECISIONS #122's established precedent for a superseded always-on
# family's task definition, explicit in DECISIONS #139) and
# south-st-pete-migrate (one-off tooling, unrelated) — not reusing either,
# per this round's explicit instruction. Recorded as DECISIONS #139,
# written and landed later in this same round, after real redeploy and
# live verification (same forward-reference precedent as #137's
# Dockerfile.migrate fix, deploy-review/crawler-review finding this
# round: don't cite a not-yet-written entry as already-decided fact).
#
# One registered revision is invoked repeatedly by the EventBridge
# Schedule (infra/register_granicus_worker_schedule.sh), every 15
# minutes, via the schedule target's TaskDefinitionArn — you do not need
# to re-run this script per invocation, only when the image tag changes
# (and the schedule needs re-pointing to the new revision afterward, same
# gap DECISIONS #137/#138 already hit for the weekly recrawl schedule).
#
# Usage:
#   IMAGE_TAG=<git-sha> infra/register_granicus_worker_taskdef.sh
# Requires: AWS_PROFILE set, .env present with RDS_MASTER_USERNAME/
# RDS_MASTER_PASSWORD/OPENAI_API_KEY/RDS_HOST, the app image (same image
# api/worker use — infra/build_push_app_image.sh) already pushed to ECR
# under the given tag, containing scripts/run_granicus_worker_once.py.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE_TAG:?Set IMAGE_TAG to the git commit SHA of the pushed app image}"
: "${AWS_PROFILE:?Set AWS_PROFILE}"

set -a
source .env
set +a
: "${RDS_HOST:?Set RDS_HOST in .env (see .env.example)}"

export AWS_DEFAULT_REGION=us-east-1

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
# Same ECR repo as api/worker (south-st-pete-app) — this task family runs
# a script inside the same application image, not the separate
# south-st-pete-app-migrate image (that one is one-off tooling with its
# own Dockerfile.migrate, unrelated to this recurring job).
IMAGE="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app:${IMAGE_TAG}"
DB_HOST="${RDS_HOST}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

OUT_FILE="${TMP_DIR}/granicus-worker-taskdef.json"
sed \
  -e "s#__IMAGE__#${IMAGE}#g" \
  -e "s#__DB_HOST__#${DB_HOST}#g" \
  -e "s#__RDS_MASTER_USERNAME__#${RDS_MASTER_USERNAME}#g" \
  -e "s#__RDS_MASTER_PASSWORD__#${RDS_MASTER_PASSWORD}#g" \
  -e "s#__OPENAI_API_KEY__#${OPENAI_API_KEY}#g" \
  "infra/granicus-worker-taskdef.template.json" > "${OUT_FILE}"

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
