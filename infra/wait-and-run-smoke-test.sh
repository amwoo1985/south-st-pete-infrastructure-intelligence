#!/bin/bash
set -e
AWS="/c/Program Files/Amazon/AWSCLIV2/aws.exe"
PROFILE="south-st-pete"

echo "Waiting for RDS instance to become available..."
until STATUS=$("$AWS" rds describe-db-instances --profile $PROFILE --db-instance-identifier south-st-pete-dev-db --query 'DBInstances[0].DBInstanceStatus' --output text 2>&1) && [ "$STATUS" = "available" ]; do
  echo "$(date '+%H:%M:%S') RDS status: $STATUS -- waiting 30s"
  sleep 30
done
echo "RDS is available."

DB_HOST=$("$AWS" rds describe-db-instances --profile $PROFILE --db-instance-identifier south-st-pete-dev-db --query 'DBInstances[0].Endpoint.Address' --output text)
echo "DB_HOST=$DB_HOST"

FARGATE_SG="sg-0b3761d661a400274"
SUBNET="subnet-0b5058ff93f41df1e"

echo "Running smoke-test task..."
TASK_ARN=$("$AWS" ecs run-task --profile $PROFILE \
  --cluster south-st-pete-dev \
  --task-definition ssp-smoke-test \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$FARGATE_SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"pg-check\",\"environment\":[{\"name\":\"DB_HOST\",\"value\":\"$DB_HOST\"}]}]}" \
  --query 'tasks[0].taskArn' --output text)
echo "TASK_ARN=$TASK_ARN"

echo "Waiting for task to stop..."
"$AWS" ecs wait tasks-stopped --profile $PROFILE --cluster south-st-pete-dev --tasks "$TASK_ARN"

echo "--- task result ---"
"$AWS" ecs describe-tasks --profile $PROFILE --cluster south-st-pete-dev --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].{ExitCode:exitCode,Reason:reason}'

TASK_ID=$(echo "$TASK_ARN" | awk -F/ '{print $NF}')
echo "--- logs ---"
sleep 5
"$AWS" logs get-log-events --profile $PROFILE \
  --log-group-name "/ecs/south-st-pete-smoke-test" \
  --log-stream-name "smoke/pg-check/$TASK_ID" \
  --query 'events[].message' --output text

echo "SMOKE_TEST_SCRIPT_DONE"
