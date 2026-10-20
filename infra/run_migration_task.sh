#!/bin/bash
# Runs the registered south-st-pete-migrate task definition as a one-off
# `aws ecs run-task`, with the actual command supplied as this script's
# arguments via ECS containerOverrides — one registered task def serves
# every migration command (DECISIONS #124), rather than re-registering
# per script.
#
# Waits for the task to reach STOPPED, prints its exit code, and prints
# its CloudWatch logs. Does NOT deregister the task definition — that's
# a separate, deliberate step (this task def carries the live RDS
# password as a plain env var; deregister promptly after you're done
# running migrations, see DECISIONS #120/#124).
#
# Usage (note the -- before the container command, so this script's own
# nothing-else-required arg list doesn't get confused with the command):
#   AWS_PROFILE=south-st-pete infra/run_migration_task.sh -- \
#     python scripts/run_embedding_pipeline.py run-all
#
#   AWS_PROFILE=south-st-pete infra/run_migration_task.sh -- \
#     sh -c "python scripts/load_granicus_export.py infra/migration_data/granicus_completed_export.json && python scripts/embed_granicus_transcripts.py"
#
# Requires: AWS_PROFILE set, south-st-pete-migrate already registered
# (infra/register_migrate_taskdef.sh), .env present with
# FARGATE_SUBNET_IDS/FARGATE_SG_ID.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1
export MSYS_NO_PATHCONV=1  # so aws.exe sees /ecs/... log-group names literally, not path-converted

if [ "${1:-}" != "--" ]; then
  echo "Usage: infra/run_migration_task.sh -- <container command...>" >&2
  exit 1
fi
shift

set -a
source .env
set +a
: "${FARGATE_SUBNET_IDS:?Set FARGATE_SUBNET_IDS in .env}"
: "${FARGATE_SG_ID:?Set FARGATE_SG_ID in .env}"

# Build the JSON command array from this script's remaining args.
COMMAND_JSON="$(printf '%s\n' "$@" | python -c 'import json,sys; print(json.dumps([l.rstrip(chr(10)) for l in sys.stdin]))')"

OVERRIDES="{\"containerOverrides\":[{\"name\":\"south-st-pete-migrate\",\"command\":${COMMAND_JSON}}]}"

TASK_ARN="$(aws ecs run-task --cluster south-st-pete-dev --task-definition south-st-pete-migrate \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${FARGATE_SUBNET_IDS}],securityGroups=[${FARGATE_SG_ID}],assignPublicIp=ENABLED}" \
  --overrides "${OVERRIDES}" \
  --query 'tasks[0].taskArn' --output text)"
echo "Task: ${TASK_ARN}"

while true; do
  STATUS="$(aws ecs describe-tasks --cluster south-st-pete-dev --tasks "${TASK_ARN}" --query 'tasks[0].lastStatus' --output text)"
  echo "  status: ${STATUS}"
  [ "${STATUS}" = "STOPPED" ] && break
  sleep 10
done

EXIT_CODE="$(aws ecs describe-tasks --cluster south-st-pete-dev --tasks "${TASK_ARN}" --query 'tasks[0].containers[0].exitCode' --output text)"
TASK_ID="${TASK_ARN##*/}"

echo "--- logs (migrate/south-st-pete-migrate/${TASK_ID}) ---"
aws logs get-log-events --log-group-name /ecs/south-st-pete-app \
  --log-stream-name "migrate/south-st-pete-migrate/${TASK_ID}" \
  --query 'events[].message' --output text

echo "--- exit code: ${EXIT_CODE} ---"
[ "${EXIT_CODE}" = "0" ]
