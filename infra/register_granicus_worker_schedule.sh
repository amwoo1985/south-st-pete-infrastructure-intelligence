#!/bin/bash
# Creates (idempotently) a scoped IAM role EventBridge Scheduler needs to
# call ecs:RunTask against ONLY the south-st-pete-granicus-worker task
# family, plus the recurring every-15-minutes schedule that invokes it —
# the scheduled replacement for the always-on south-st-pete-worker
# service. Recorded as DECISIONS #139, written and landed later in this
# same round, after real redeploy and live verification (see
# infra/register_granicus_worker_taskdef.sh's header for the
# forward-reference precedent this follows).
#
# Mirrors infra/register_recrawl_schedule.sh's pattern (same IAM
# create-or-update shape, same cygpath fix, same idempotent
# create-or-update-schedule shape) but is its OWN schedule name and its
# OWN execution role — deliberately NOT reusing
# south-st-pete-scheduler-execution-role (the recrawl schedule's role):
# that role's policy is scoped to ecs:RunTask against
# south-st-pete-migrate only (infra/recrawl-scheduler-permissions-policy.template.json),
# and this round's brief was explicit about not granting broader
# ecs:RunTask than the one new task family actually needs (DECISIONS
# #127's narrower-option precedent). A shared role would mean either
# widening that existing policy's Resource to cover a second family (more
# than either schedule needs) or leaving it narrow and having this
# schedule silently fail at invoke time — a new, separately scoped role
# avoids both.
#
# Usage:
#   AWS_PROFILE=south-st-pete infra/register_granicus_worker_schedule.sh
#
# Requires: AWS_PROFILE set, .env present with FARGATE_SUBNET_IDS/
# FARGATE_SG_ID, south-st-pete-granicus-worker task definition already
# registered with an image that contains
# scripts/run_granicus_worker_once.py
# (infra/build_push_app_image.sh + infra/register_granicus_worker_taskdef.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1

set -a
source .env
set +a
: "${FARGATE_SUBNET_IDS:?Set FARGATE_SUBNET_IDS in .env}"
: "${FARGATE_SG_ID:?Set FARGATE_SG_ID in .env}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_NAME="south-st-pete-granicus-scheduler-execution-role"
EXECUTION_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
TASK_EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/south-st-pete-ecs-task-execution-role"
SCHEDULE_NAME="south-st-pete-granicus-worker-schedule"
SCHEDULE_GROUP="default"
SCHEDULE_ARN="arn:aws:scheduler:us-east-1:${ACCOUNT_ID}:schedule/${SCHEDULE_GROUP}/${SCHEDULE_NAME}"
CLUSTER_ARN="arn:aws:ecs:us-east-1:${ACCOUNT_ID}:cluster/south-st-pete-dev"
TASKDEF_FAMILY_ARN="arn:aws:ecs:us-east-1:${ACCOUNT_ID}:task-definition/south-st-pete-granicus-worker:*"

# EventBridge Scheduler's EcsParameters.TaskDefinitionArn requires a
# fully revision-qualified ARN (confirmed live pattern this project
# already hit for the recrawl schedule, DECISIONS #125/#137/#138) —
# resolved live here to the actual latest ACTIVE revision, not guessed.
TASKDEF_ARN_LATEST="$(aws ecs describe-task-definition \
  --task-definition south-st-pete-granicus-worker \
  --query 'taskDefinition.taskDefinitionArn' --output text)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- 1. Scoped IAM role EventBridge Scheduler assumes to call ecs:RunTask ---
sed \
  -e "s#__ACCOUNT_ID__#${ACCOUNT_ID}#g" \
  -e "s#__SCHEDULE_ARN__#${SCHEDULE_ARN}#g" \
  infra/granicus-scheduler-trust-policy.template.json > "${TMP_DIR}/trust-policy.json"
sed \
  -e "s#__TASKDEF_FAMILY_ARN__#${TASKDEF_FAMILY_ARN}#g" \
  -e "s#__CLUSTER_ARN__#${CLUSTER_ARN}#g" \
  -e "s#__TASK_EXEC_ROLE_ARN__#${TASK_EXEC_ROLE_ARN}#g" \
  infra/granicus-scheduler-permissions-policy.template.json > "${TMP_DIR}/permissions-policy.json"

TRUST_FILE="${TMP_DIR}/trust-policy.json"
PERM_FILE="${TMP_DIR}/permissions-policy.json"
SCHEDULE_REQ_FILE="${TMP_DIR}/schedule-request.json"
if command -v cygpath >/dev/null 2>&1; then
  TRUST_FILE="$(cygpath -m "${TRUST_FILE}")"
  PERM_FILE="$(cygpath -m "${PERM_FILE}")"
  SCHEDULE_REQ_FILE="$(cygpath -m "${SCHEDULE_REQ_FILE}")"
fi

# Idempotency check (.claude/rules/deploy.md: "pre-check before any
# external write") — create-role fails loud on a duplicate name otherwise.
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "Role ${ROLE_NAME} already exists, updating trust policy only."
  aws iam update-assume-role-policy --role-name "${ROLE_NAME}" \
    --policy-document "file://${TRUST_FILE}" >/dev/null
else
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${TRUST_FILE}" >/dev/null
fi
# put-role-policy is itself idempotent (upsert-by-name) — safe to always run.
aws iam put-role-policy --role-name "${ROLE_NAME}" \
  --policy-name south-st-pete-scheduler-run-granicus-worker-task \
  --policy-document "file://${PERM_FILE}"

# --- 2. The recurring schedule itself ---
# rate(15 minutes): this round's approved cadence (see DECISIONS #139) —
# frequent
# enough that a newly-registered meeting (or a stale claim recovered by
# recover_stale_claims, see scripts/run_granicus_worker_once.py) starts
# processing within a normal working session, same "starts within a
# session" bar infra/../app/granicus/run_worker.py's own POLL_INTERVAL_SECONDS
# reasoning used, just satisfied by scheduled-invocation cadence instead
# of a poll loop this time.
FARGATE_SUBNET_IDS="${FARGATE_SUBNET_IDS}" FARGATE_SG_ID="${FARGATE_SG_ID}" \
ACCOUNT_ID="${ACCOUNT_ID}" EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN}" \
CLUSTER_ARN="${CLUSTER_ARN}" TASKDEF_ARN_LATEST="${TASKDEF_ARN_LATEST}" \
SCHEDULE_NAME="${SCHEDULE_NAME}" SCHEDULE_GROUP="${SCHEDULE_GROUP}" \
python - "${SCHEDULE_REQ_FILE}" <<'PYEOF'
import json, os, sys

out_path = sys.argv[1]
subnets = [s for s in os.environ["FARGATE_SUBNET_IDS"].split(",") if s]

request = {
    "Name": os.environ["SCHEDULE_NAME"],
    "GroupName": os.environ["SCHEDULE_GROUP"],
    "ScheduleExpression": "rate(15 minutes)",
    "FlexibleTimeWindow": {"Mode": "OFF"},
    "Target": {
        "Arn": os.environ["CLUSTER_ARN"],
        "RoleArn": os.environ["EXECUTION_ROLE_ARN"],
        "EcsParameters": {
            "TaskDefinitionArn": os.environ["TASKDEF_ARN_LATEST"],
            "LaunchType": "FARGATE",
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": subnets,
                    "SecurityGroups": [os.environ["FARGATE_SG_ID"]],
                    "AssignPublicIp": "ENABLED",
                }
            },
        },
    },
}
with open(out_path, "w") as f:
    json.dump(request, f)
PYEOF

# Idempotency pre-check (.claude/rules/deploy.md) — aws scheduler
# create-schedule is not idempotent by name; update-schedule accepts the
# same request shape, so re-running this script after a new image/taskdef
# revision re-points the existing schedule rather than erroring.
if aws scheduler get-schedule --name "${SCHEDULE_NAME}" --group-name "${SCHEDULE_GROUP}" >/dev/null 2>&1; then
  echo "Schedule ${SCHEDULE_NAME} already exists, updating in place."
  aws scheduler update-schedule --cli-input-json "file://${SCHEDULE_REQ_FILE}"
else
  aws scheduler create-schedule --cli-input-json "file://${SCHEDULE_REQ_FILE}"
fi

echo "Schedule created/updated: ${SCHEDULE_ARN}"
