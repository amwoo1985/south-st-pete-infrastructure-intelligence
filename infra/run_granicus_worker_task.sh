#!/bin/bash
# Manually runs the registered south-st-pete-granicus-worker task
# definition as a one-off `aws ecs run-task`, using the operator's own
# AWS credentials — no EventBridge Scheduler, no new IAM role.
#
# This replaces the EventBridge-Scheduler-triggered cadence that
# DECISIONS #139 planned but never actually landed:
# `aws scheduler create-schedule` hit a persistent ValidationException
# ("execution role must allow Scheduler to assume the role") across
# three independently tested IAM trust-policy shapes (DECISIONS #139,
# #150, #155 — including the shape AWS Support reportedly recommended),
# with the real root cause never resolved. DECISIONS #155 flagged this
# manual-trigger approach as a proposal pending Amber's call, not yet
# authorized at that point. Per DECISIONS #156, Amber reviewed that
# tradeoff directly and gave explicit sign-off to build and run this
# script: run the task by hand, whenever a new meeting has been
# registered (scripts/register_granicus_meeting.py is already a
# 100%-manual step, so this fits the existing operational pattern)
# rather than on a schedule.
#
# This script is NEVER cron'd or scheduled by this repo. It is run by a
# human, by hand, after registering a meeting. Do not wire it into
# EventBridge, cron, a CI job, or anything else that invokes it
# unattended — that reintroduces the exact problem #139/#150/#155
# couldn't safely solve (an execution identity other than the
# operator's own that needs its own IAM trust relationship).
#
# Mirrors infra/run_migration_task.sh's one-off-task-runner pattern
# (run-task -> poll describe-tasks until STOPPED -> exit code -> logs ->
# fail loud on nonzero exit), and infra/register_granicus_worker_schedule.sh's
# discipline of resolving the task definition's live latest-ACTIVE-revision
# ARN rather than guessing a bare family name. Unlike run_migration_task.sh,
# no containerOverrides are passed — this task family has exactly one job
# (python scripts/run_granicus_worker_once.py, baked into the registered
# task def) and no command needs to be supplied per-run.
#
# Idempotency / double-launch guard (.claude/rules/deploy.md: "pre-check
# before any external write" — run-task itself has no idempotency key,
# so this list-tasks check against RUNNING tasks in the same family is
# the pre-check): if a run is already in flight, this script refuses to
# launch a second one and exits nonzero. The worker's own Postgres
# advisory lock (_SINGLE_FLIGHT_LOCK_KEY in
# scripts/run_granicus_worker_once.py) would make a second concurrent
# run a fast DB-safe no-op anyway, but launching one regardless still
# wastes Fargate cost and doubles load on Granicus's CDN for no benefit
# — so this guard stops it before the AWS API call, not after.
#
# Usage:
#   AWS_PROFILE=south-st-pete infra/run_granicus_worker_task.sh
#
# Requires: AWS_PROFILE set (the "south-st-pete" profile in
# ~/.aws/credentials, with existing ecs:RunTask/ecs:ListTasks/
# ecs:DescribeTasks/ecs:DescribeTaskDefinition/logs:GetLogEvents
# permissions — this script grants no new IAM permissions; if the
# profile lacks one of these, that's an
# account-level change needing explicit sign-off per DECISIONS #119,
# flag it rather than granting it here), .env present with
# FARGATE_SUBNET_IDS/FARGATE_SG_ID, south-st-pete-granicus-worker task
# definition already registered (infra/register_granicus_worker_taskdef.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1
export MSYS_NO_PATHCONV=1  # so aws.exe sees /ecs/... log-group names literally, not path-converted

set -a
source .env
set +a
: "${FARGATE_SUBNET_IDS:?Set FARGATE_SUBNET_IDS in .env}"
: "${FARGATE_SG_ID:?Set FARGATE_SG_ID in .env}"

CLUSTER="south-st-pete-dev"
FAMILY="south-st-pete-granicus-worker"
CONTAINER_NAME="south-st-pete-granicus-worker"

# --- Double-launch guard (idempotency pre-check, deploy.md) ---
RUNNING_TASKS="$(aws ecs list-tasks --cluster "${CLUSTER}" --family "${FAMILY}" \
  --desired-status RUNNING --query 'taskArns' --output text)"
if [ -n "${RUNNING_TASKS}" ] && [ "${RUNNING_TASKS}" != "None" ]; then
  echo "A ${FAMILY} task is already running (${RUNNING_TASKS})." >&2
  echo "Not launching a second one -- it would waste Fargate cost and double" >&2
  echo "load on Granicus's CDN, even though the worker's own Postgres advisory" >&2
  echo "lock (_SINGLE_FLIGHT_LOCK_KEY) would make the second run a fast" >&2
  echo "DB-safe no-op. Wait for the in-flight run to finish, then re-run this script." >&2
  exit 1
fi

# EventBridge Scheduler's EcsParameters.TaskDefinitionArn required a
# fully revision-qualified ARN (DECISIONS #125/#137/#138) and the same
# discipline applies to a plain run-task call: resolve the actual latest
# ACTIVE revision live rather than guessing a bare family name.
TASKDEF_ARN_LATEST="$(aws ecs describe-task-definition \
  --task-definition "${FAMILY}" \
  --query 'taskDefinition.taskDefinitionArn' --output text)"
echo "Task definition: ${TASKDEF_ARN_LATEST}"

TASK_ARN="$(aws ecs run-task --cluster "${CLUSTER}" --task-definition "${TASKDEF_ARN_LATEST}" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${FARGATE_SUBNET_IDS}],securityGroups=[${FARGATE_SG_ID}],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)"
echo "Task: ${TASK_ARN}"

# Bounded wait, not an unconditional `while true` — a task stuck in
# PROVISIONING/PENDING (bad subnet, bad SG, image pull failure) must
# fail loud instead of hanging this script forever (crawler.md's
# fail-loud convention applied here too: silence on a stuck task would
# look identical to "still working" to whoever runs this by hand).
# 7200s/2h is a generous ceiling: this task can legitimately run long
# (recover_stale_claims + up to 2 real transcription jobs via the
# OpenAI API against multi-hour meeting audio, DECISIONS #137/#138/#139
# measured real completions in that range), not a quick health check.
MAX_WAIT_SECONDS=7200
ELAPSED=0
while true; do
  STATUS="$(aws ecs describe-tasks --cluster "${CLUSTER}" --tasks "${TASK_ARN}" --query 'tasks[0].lastStatus' --output text)"
  echo "  status: ${STATUS}"
  [ "${STATUS}" = "STOPPED" ] && break
  if [ "${ELAPSED}" -ge "${MAX_WAIT_SECONDS}" ]; then
    echo "Task ${TASK_ARN} did not reach STOPPED within ${MAX_WAIT_SECONDS}s (stuck at ${STATUS}) -- failing loud instead of waiting forever. Check the task in the ECS console." >&2
    exit 1
  fi
  sleep 10
  ELAPSED=$((ELAPSED + 10))
done

EXIT_CODE="$(aws ecs describe-tasks --cluster "${CLUSTER}" --tasks "${TASK_ARN}" --query 'tasks[0].containers[0].exitCode' --output text)"
TASK_ID="${TASK_ARN##*/}"

# Stream name = <awslogs-stream-prefix>/<container-name>/<task-id>; the
# prefix is "granicus-worker" per infra/granicus-worker-taskdef.template.json,
# not the family name or script name.
echo "--- logs (granicus-worker/${CONTAINER_NAME}/${TASK_ID}) ---"
aws logs get-log-events --log-group-name /ecs/south-st-pete-app \
  --log-stream-name "granicus-worker/${CONTAINER_NAME}/${TASK_ID}" \
  --query 'events[].message' --output text

echo "--- exit code: ${EXIT_CODE} ---"
[ "${EXIT_CODE}" = "0" ]
