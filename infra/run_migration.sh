#!/bin/bash
# Runs `python -m app.db.migrate <command>` against RDS as a one-off Fargate
# task (DECISIONS #160), then prints its log output and exit code.
#
#   status  list applied / pending migrations (changes nothing)
#   report  read-only summary of data-table contents (live and archived)
#   up      apply pending migrations (as the RDS master user)
#
# Usage:
#   AWS_PROFILE=south-st-pete infra/run_migration.sh status|report|up
# Requires: the south-st-pete-migrate task definition registered by
# infra/register_app_taskdefs.sh for the image you're deploying.
#
# Deploy order for a change that adds migrations:
#   1. build + push the image          (infra/build_push_app_image.sh)
#   2. register task definitions       (infra/register_app_taskdefs.sh)
#   3. infra/run_migration.sh up       (schema first; the old api keeps working
#                                       as long as migrations are additive)
#   4. update the api service to the new task definition
# The new api refuses to boot if step 3 hasn't run (assert_schema_current).
set -euo pipefail
cd "$(dirname "$0")/.."

COMMAND="${1:-}"
case "$COMMAND" in status|report|up) ;; *) echo "usage: $0 status|report|up" >&2; exit 2;; esac
: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
# Git Bash would otherwise rewrite /ecs/... log-group arguments into
# Windows paths.
export MSYS_NO_PATHCONV=1

CLUSTER=south-st-pete-dev
FAMILY=south-st-pete-migrate
CONTAINER=south-st-pete-migrate
LOG_GROUP=/ecs/south-st-pete-app

# Same subnets/security group as the running api service (which already
# reaches RDS), read live rather than duplicated into .env.
NETWORK="$(aws ecs describe-services --cluster "$CLUSTER" --services south-st-pete-api \
  --query 'services[0].networkConfiguration' --output json)"
if [ -z "$NETWORK" ] || [ "$NETWORK" = "null" ]; then
  echo "Could not read the api service's network configuration." >&2
  exit 1
fi

OVERRIDES="{\"containerOverrides\":[{\"name\":\"${CONTAINER}\",\"command\":[\"python\",\"-m\",\"app.db.migrate\",\"${COMMAND}\"]}]}"

TASK_ARN="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" \
  --launch-type FARGATE --network-configuration "$NETWORK" --overrides "$OVERRIDES" \
  --query 'tasks[0].taskArn' --output text)"
if [ -z "$TASK_ARN" ] || [ "$TASK_ARN" = "None" ]; then
  echo "run-task did not start a task." >&2
  exit 1
fi
TASK_ID="${TASK_ARN##*/}"
echo "started migrate task ${TASK_ID} (${COMMAND}); waiting for it to stop..."

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

EXIT_CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?name=='${CONTAINER}'].exitCode | [0]" --output text)"
STOP_REASON="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].stoppedReason' --output text)"

echo "----- task output -----"
aws logs get-log-events --log-group-name "$LOG_GROUP" \
  --log-stream-name "migrate/${CONTAINER}/${TASK_ID}" --start-from-head \
  --query 'events[].message' --output text | tr '\t' '\n' || echo "(no log output found)"
echo "-----------------------"
echo "exit code: ${EXIT_CODE}   stopped reason: ${STOP_REASON}"

if [ "$EXIT_CODE" != "0" ]; then
  exit 1
fi
