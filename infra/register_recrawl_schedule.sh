#!/bin/bash
# Creates (idempotently) the scoped IAM role EventBridge Scheduler needs to
# call ecs:RunTask on this project's behalf, plus the recurring
# "every Saturday" schedule that runs the live recrawl of this project's
# ~8 authorized web-page sources (scripts/run_live_recrawl.py).
#
# Reuses the existing south-st-pete-migrate task definition/image
# (infra/Dockerfile.migrate, DECISIONS #124) rather than a new task
# family — deliberate, not a shortcut: that image already COPYs the
# whole scripts/ tree (so scripts/run_live_recrawl.py lands in it
# automatically once committed and the image is rebuilt, no Dockerfile
# change needed), and its assignPublicIp=ENABLED network config already
# has proven outbound egress to external hosts (OpenAI, Granicus CDN) —
# the same kind of reachability a live web-page recrawl needs. Registering
# a second near-identical task family/ECR repo for "run a different
# script in the same image" would duplicate infra for no real benefit.
# The container command (which script + subcommand to run) is supplied
# at schedule-target time via containerOverrides, exactly like
# infra/run_migration_task.sh already does for on-demand migration runs —
# one registered task def, many different invocations.
#
# REAL TRADEOFF, flagged plainly rather than buried (deploy-review
# finding, this round): infra/DEPLOY_NEXT_STEPS.md documents
# south-st-pete-migrate's convention as "deregister promptly after use"
# because it carries the live RDS master password as a plain env var
# (DECISIONS #120/#124 — no Secrets Manager/SSM access on this IAM
# user). A RECURRING weekly schedule needs this task definition to stay
# an ACTIVE, registered revision indefinitely — it cannot be
# deregistered after each Saturday run without breaking the next one.
# That drops the "register→run→deregister, minimize the exposure
# window" mitigation this specific task family was previously operated
# under. This is NOT a new category of risk versus what already exists
# (the live south-st-pete-api/south-st-pete-worker ECS services already
# run permanently-registered ACTIVE task defs carrying the same plain
# RDS password, unconditionally, since DECISIONS #122) — but it does
# formally extend that same accepted tradeoff to a third always-on
# task family, and that extension has not had its own DECISIONS.md
# entry or Amber's explicit sign-off. Flagged to the orchestrator this
# round for exactly that; not something this script silently decided.
#
# NOT RUN THIS SESSION, on purpose, in two layers:
#   1. `aws scheduler create-schedule` — confirmed BLOCKED. This IAM user
#      (south-st-pete-infra-dev) has zero EventBridge Scheduler
#      permissions (scheduler:ListSchedules denied, live-confirmed). This
#      script's final step will fail until that gap is closed:
#        aws iam attach-user-policy --user-name south-st-pete-infra-dev \
#          --policy-arn arn:aws:iam::aws:policy/AmazonEventBridgeSchedulerFullAccess
#   2. `aws iam create-role`/`put-role-policy` for the scoped scheduler
#      execution role below — NOT independently blocked (this user already
#      has IAMFullAccess and this call would very likely succeed today) —
#      but deliberately left unexecuted anyway. Per DECISIONS #119's own
#      precedent, changes to this AWS account's IAM surface get Amber's
#      explicit in-session sign-off before they happen, not a unilateral
#      call by whichever session judges a given role "scoped enough" to be
#      lower-risk. This script is ready to run the moment that sign-off
#      (and the scheduler policy above) are both given — see this round's
#      deploy-infra report for the full reasoning on where that line was
#      drawn.
#
# Usage (once both of the above are authorized):
#   AWS_PROFILE=south-st-pete infra/register_recrawl_schedule.sh
#
# Requires: AWS_PROFILE set, .env present with FARGATE_SUBNET_IDS/
# FARGATE_SG_ID, south-st-pete-migrate task definition already registered
# with an image that contains scripts/run_live_recrawl.py
# (infra/build_push_migrate_image.sh + infra/register_migrate_taskdef.sh).
#
# Optional .env override:
#   RECRAWL_SUBCOMMAND=run   # default — scripts/run_live_recrawl.py's real,
#                             # landed CLI is `estimate`/`run` (no separate
#                             # slice mode: its own docstring explains why
#                             # run_embedding_pipeline.py's run-slice/run-all
#                             # caution about a big first-time bill doesn't
#                             # apply to a recurring, mostly-idempotent-skip
#                             # weekly run). Override here only if that CLI
#                             # changes again later.
#
# The migrate task definition's env vars (LOCAL_DB_HOST etc, pointed at the
# real RDS endpoint — infra/register_migrate_taskdef.sh) are what make
# `run` write to RDS despite run_live_recrawl.py's own docstring describing
# its *default* as the local docker-compose Postgres at 127.0.0.1 — same
# env-var-override mechanism DECISIONS #124 already used to point
# run_embedding_pipeline.py/embed_granicus_transcripts.py at RDS instead
# of their own local-default connection target.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1

set -a
source .env
set +a
: "${FARGATE_SUBNET_IDS:?Set FARGATE_SUBNET_IDS in .env}"
: "${FARGATE_SG_ID:?Set FARGATE_SG_ID in .env}"
RECRAWL_SUBCOMMAND="${RECRAWL_SUBCOMMAND:-run}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ROLE_NAME="south-st-pete-scheduler-execution-role"
EXECUTION_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
TASK_EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/south-st-pete-ecs-task-execution-role"
SCHEDULE_NAME="south-st-pete-weekly-recrawl"
SCHEDULE_GROUP="default"
SCHEDULE_ARN="arn:aws:scheduler:us-east-1:${ACCOUNT_ID}:schedule/${SCHEDULE_GROUP}/${SCHEDULE_NAME}"
CLUSTER_ARN="arn:aws:ecs:us-east-1:${ACCOUNT_ID}:cluster/south-st-pete-dev"
TASKDEF_FAMILY_ARN="arn:aws:ecs:us-east-1:${ACCOUNT_ID}:task-definition/south-st-pete-migrate:*"

# deploy-review finding: EventBridge Scheduler's EcsParameters.TaskDefinitionArn
# is commonly documented as requiring a fully revision-qualified ARN, unlike
# `ecs run-task --task-definition <family>` (infra/run_migration_task.sh's
# pattern), which resolves the family name to its latest ACTIVE revision on
# AWS's side. Resolved live here rather than guessed, matching this repo's
# own "verified, not assumed" standard (DECISIONS #104/#125) — the exact
# ARN in use is always the actual latest ACTIVE revision at schedule-create
# time, not a bare family name whose behavior in this API isn't confirmed.
TASKDEF_ARN_LATEST="$(aws ecs describe-task-definition \
  --task-definition south-st-pete-migrate \
  --query 'taskDefinition.taskDefinitionArn' --output text)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- 1. Scoped IAM role EventBridge Scheduler assumes to call ecs:RunTask ---
sed \
  -e "s#__ACCOUNT_ID__#${ACCOUNT_ID}#g" \
  -e "s#__SCHEDULE_ARN__#${SCHEDULE_ARN}#g" \
  infra/recrawl-scheduler-trust-policy.template.json > "${TMP_DIR}/trust-policy.json"
sed \
  -e "s#__TASKDEF_FAMILY_ARN__#${TASKDEF_FAMILY_ARN}#g" \
  -e "s#__CLUSTER_ARN__#${CLUSTER_ARN}#g" \
  -e "s#__TASK_EXEC_ROLE_ARN__#${TASK_EXEC_ROLE_ARN}#g" \
  infra/recrawl-scheduler-permissions-policy.template.json > "${TMP_DIR}/permissions-policy.json"

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
  --policy-name south-st-pete-scheduler-run-recrawl-task \
  --policy-document "file://${PERM_FILE}"

# --- 2. The recurring schedule itself ---
# Built via python, not hand-escaped shell JSON, for the same reason
# infra/run_migration_task.sh builds its command array that way — a
# dynamic-length subnet list (FARGATE_SUBNET_IDS) does not sed cleanly,
# and this repo's own DECISIONS #122/#123 history is a record of what
# hand-built JSON/path escaping bugs cost when they slip through.
#
# cron(0 6 ? * SAT *): every Saturday at 06:00 UTC (02:00 America/New_York
# during EDT, 01:00 during EST) — early morning, before any weekend
# review of the week's crawl, and off the RDS/Fargate always-on
# baseline's traffic hours (there is none, but keeps the crawl's own
# request burst away from any daytime interactive /query usage). Pick a
# different hour/day in .env-driven override if this default doesn't fit
# once real usage patterns are known — not hardcoded elsewhere, only here.
FARGATE_SUBNET_IDS="${FARGATE_SUBNET_IDS}" FARGATE_SG_ID="${FARGATE_SG_ID}" \
ACCOUNT_ID="${ACCOUNT_ID}" EXECUTION_ROLE_ARN="${EXECUTION_ROLE_ARN}" \
CLUSTER_ARN="${CLUSTER_ARN}" TASKDEF_ARN_LATEST="${TASKDEF_ARN_LATEST}" \
SCHEDULE_NAME="${SCHEDULE_NAME}" SCHEDULE_GROUP="${SCHEDULE_GROUP}" \
RECRAWL_SUBCOMMAND="${RECRAWL_SUBCOMMAND}" \
python - "${SCHEDULE_REQ_FILE}" <<'PYEOF'
import json, os, sys

out_path = sys.argv[1]
subnets = [s for s in os.environ["FARGATE_SUBNET_IDS"].split(",") if s]

request = {
    "Name": os.environ["SCHEDULE_NAME"],
    "GroupName": os.environ["SCHEDULE_GROUP"],
    "ScheduleExpression": "cron(0 6 ? * SAT *)",
    "ScheduleExpressionTimezone": "UTC",
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
        "Input": json.dumps(
            {
                "containerOverrides": [
                    {
                        "name": "south-st-pete-migrate",
                        "command": [
                            "python",
                            "scripts/run_live_recrawl.py",
                            os.environ["RECRAWL_SUBCOMMAND"],
                        ],
                    }
                ]
            }
        ),
    },
}
with open(out_path, "w") as f:
    json.dump(request, f)
PYEOF

# Idempotency pre-check (.claude/rules/deploy.md: "idempotency key +
# pre-check before any external write") — unlike the IAM role section
# above (create-role/update-assume-role-policy is a natural get-or-create
# pair), aws scheduler create-schedule is NOT idempotent by name: a
# re-run of this script (e.g. to pick up a new RECRAWL_SUBCOMMAND or a
# changed task-def revision) would hit a duplicate-name conflict without
# this check. update-schedule accepts the same request shape as
# create-schedule (both take Name/GroupName/ScheduleExpression/
# FlexibleTimeWindow/Target), so the same generated request file serves
# either call.
if aws scheduler get-schedule --name "${SCHEDULE_NAME}" --group-name "${SCHEDULE_GROUP}" >/dev/null 2>&1; then
  echo "Schedule ${SCHEDULE_NAME} already exists, updating in place."
  aws scheduler update-schedule --cli-input-json "file://${SCHEDULE_REQ_FILE}"
else
  aws scheduler create-schedule --cli-input-json "file://${SCHEDULE_REQ_FILE}"
fi

echo "Schedule created/updated: ${SCHEDULE_ARN}"
