#!/bin/bash
# Runs the operator tool `python -m app.admin <args...>` against RDS as a
# one-off Fargate task (DECISIONS #174), using the migrate task definition (it
# already holds the database owner login), then prints the task's output and
# exit code. Nothing in the tool talks to Cognito.
#
#   AWS_PROFILE=south-st-pete infra/run_admin.sh create-user --cognito-sub <uuid> --email <addr> [--platform-admin] --reason "..." [--apply]
#   AWS_PROFILE=south-st-pete infra/run_admin.sh create-org --legal-name "..." --as <operator email> --reason "..." [--apply]
#   AWS_PROFILE=south-st-pete infra/run_admin.sh grant-org-admin --org <org id> --user <email> --as <operator email> --reason "..." [--apply]
#
# Without --apply each command is a dry run (the database's rules run, then it
# rolls back). Arguments are passed to the container as a JSON list built by
# python, so spaces and quotes in names and reasons are safe. Needs `aws` and a
# working `python` on the PATH (in Git Bash the repo's .venv python is fine; the
# Windows Store "python3" stub is not).
# The arguments also appear in the CloudWatch log and the ECS task record, so never put a secret in them.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in create-user|create-org|grant-org-admin) ;; *) echo "usage: $0 create-user|create-org|grant-org-admin <options>" >&2; exit 2;; esac
: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
export MSYS_NO_PATHCONV=1

CLUSTER=south-st-pete-dev
FAMILY=south-st-pete-migrate
CONTAINER=south-st-pete-migrate
LOG_GROUP=/ecs/south-st-pete-app

NETWORK="$(aws ecs describe-services --cluster "$CLUSTER" --services south-st-pete-api \
  --query 'services[0].networkConfiguration' --output json)"
if [ -z "$NETWORK" ] || [ "$NETWORK" = "null" ]; then
  echo "Could not read the api service's network configuration." >&2
  exit 1
fi

# The caller's AWS identity is appended to --reason, so the audit row records
# who really ran the task (--as is only a typed attribution).
CALLER="$(aws sts get-caller-identity --query Arn --output text)"

OVERRIDES="$(python - "$CONTAINER" "$CALLER" "$@" <<'PY'
import json, sys
container, caller, args = sys.argv[1], sys.argv[2], list(sys.argv[3:])
if any(a.startswith("--reason=") for a in args):
    sys.exit("write --reason as two words (--reason \"text\"), not --reason=text")
found = [i for i, a in enumerate(args) if a == "--reason"]
if len(found) != 1 or found[0] + 1 >= len(args) or args[found[0] + 1].startswith("--"):
    sys.exit("give --reason exactly once, followed by its text")
text = args[found[0] + 1].strip()
if not text or len(text) > 300:
    sys.exit("--reason must be 1-300 characters")
args[found[0] + 1] = text + " [run by " + caller + "]"
print(json.dumps({"containerOverrides": [{"name": container, "command": ["python", "-m", "app.admin", *args]}]}))
PY
)"

TASK_ARN="$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" \
  --launch-type FARGATE --network-configuration "$NETWORK" --overrides "$OVERRIDES" \
  --query 'tasks[0].taskArn' --output text)"
if [ -z "$TASK_ARN" ] || [ "$TASK_ARN" = "None" ]; then
  echo "run-task did not start a task." >&2
  exit 1
fi
TASK_ID="${TASK_ARN##*/}"
echo "started admin task ${TASK_ID} ($1); waiting for it to stop..."

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
