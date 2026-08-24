#!/bin/bash
# Creates (idempotently) an SNS topic + email subscription and an AWS
# Budgets monthly cost budget with 50/90/100% notification thresholds
# wired to that topic — CLAUDE.md's own "set a budget alert before the
# unattended full [Granicus] backfill runs" requirement, and the general
# cost-visibility need for a solo-dev account with real recurring
# Fargate/RDS spend plus per-minute whisper-1 transcription cost.
#
# NOT RUN THIS SESSION. Confirmed live this round:
#   - `sns:CreateTopic` DENIED for south-st-pete-infra-dev (this IAM user
#     can currently only sns:ListTopics, granted incidentally by
#     AmazonECS_FullAccess's own policy document).
#   - `budgets:ViewBudget` DENIED, and there is no single AWS-managed
#     "budgets full access" policy to attach (AWS only ships
#     AWSBudgetsReadOnlyAccess and narrower automated-action policies
#     that don't cover budgets:CreateBudget/ModifyBudget).
# Closing both gaps (needs Amber's sign-off, same as every other IAM
# change in this project per DECISIONS #119's precedent):
#   aws iam attach-user-policy --user-name south-st-pete-infra-dev \
#     --policy-arn arn:aws:iam::aws:policy/AmazonSNSFullAccess
#   aws iam create-policy --policy-name south-st-pete-budgets-management \
#     --policy-document file://infra/budgets-iam-policy.json
#   aws iam attach-user-policy --user-name south-st-pete-infra-dev \
#     --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/south-st-pete-budgets-management
#
# Usage (once both are attached):
#   AWS_PROFILE=south-st-pete infra/register_cost_alerts.sh
#
# Requires: AWS_PROFILE set, .env present with BUDGET_ALERT_EMAIL and
# MONTHLY_BUDGET_USD (see .env.example — MONTHLY_BUDGET_USD has no
# invented default; Amber picks the real figure, see this round's
# deploy-infra report for reasoned candidates).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1

set -a
source .env
set +a
: "${BUDGET_ALERT_EMAIL:?Set BUDGET_ALERT_EMAIL in .env}"
: "${MONTHLY_BUDGET_USD:?Set MONTHLY_BUDGET_USD in .env}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TOPIC_NAME="south-st-pete-cost-alerts"
BUDGET_NAME="south-st-pete-monthly-cost-budget"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# --- 1. SNS topic + email subscription ---
# sns:create-topic is itself idempotent by name (returns the existing
# ARN if the topic already exists) — no separate pre-check needed here,
# matching .claude/rules/deploy.md's idempotency rule.
TOPIC_ARN="$(aws sns create-topic --name "${TOPIC_NAME}" --query 'TopicArn' --output text)"
echo "SNS topic: ${TOPIC_ARN}"

# subscribe is also idempotent for the same (topic, protocol, endpoint)
# triple — AWS returns the existing subscription rather than duplicating.
aws sns subscribe --topic-arn "${TOPIC_ARN}" --protocol email \
  --notification-endpoint "${BUDGET_ALERT_EMAIL}" >/dev/null
echo "Subscribed ${BUDGET_ALERT_EMAIL} — AWS will send a confirmation email that must be clicked before alerts deliver."

# AWS Budgets requires an explicit resource policy on the SNS topic
# granting the budgets service permission to publish to it — without
# this, notifications are configured but silently never arrive (a real,
# documented AWS gotcha, not an edge case being over-handled).
TOPIC_POLICY_FILE="${TMP_DIR}/sns-topic-policy.json"
ACCOUNT_ID="${ACCOUNT_ID}" TOPIC_ARN="${TOPIC_ARN}" python - "${TOPIC_POLICY_FILE}" <<'PYEOF'
import json, os, sys

out_path = sys.argv[1]
topic_arn = os.environ["TOPIC_ARN"]
account_id = os.environ["ACCOUNT_ID"]

policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowBudgetsToPublish",
            "Effect": "Allow",
            "Principal": {"Service": "budgets.amazonaws.com"},
            "Action": "SNS:Publish",
            "Resource": topic_arn,
            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
        }
    ],
}
with open(out_path, "w") as f:
    json.dump(policy, f)
PYEOF

if command -v cygpath >/dev/null 2>&1; then
  TOPIC_POLICY_FILE="$(cygpath -m "${TOPIC_POLICY_FILE}")"
fi
aws sns set-topic-attributes --topic-arn "${TOPIC_ARN}" \
  --attribute-name Policy --attribute-value "file://${TOPIC_POLICY_FILE}"

# --- 2. Monthly cost budget with 50/90/100% thresholds ---
# Pre-check before create (.claude/rules/deploy.md: "idempotency key +
# pre-check before any external write") — create-budget is NOT
# idempotent by name; it raises DuplicateRecordException on a re-run,
# unlike sns:create-topic above.
if aws budgets describe-budget --account-id "${ACCOUNT_ID}" \
    --budget-name "${BUDGET_NAME}" >/dev/null 2>&1; then
  echo "Budget ${BUDGET_NAME} already exists — skipping create. Delete it first (aws budgets delete-budget) to re-create with a new MONTHLY_BUDGET_USD, or use the AWS Console to edit the existing threshold in place."
  exit 0
fi

BUDGET_REQ_FILE="${TMP_DIR}/budget-request.json"
ACCOUNT_ID="${ACCOUNT_ID}" BUDGET_NAME="${BUDGET_NAME}" \
MONTHLY_BUDGET_USD="${MONTHLY_BUDGET_USD}" TOPIC_ARN="${TOPIC_ARN}" \
python - "${BUDGET_REQ_FILE}" <<'PYEOF'
import json, os, sys

out_path = sys.argv[1]
account_id = os.environ["ACCOUNT_ID"]
topic_arn = os.environ["TOPIC_ARN"]
amount = os.environ["MONTHLY_BUDGET_USD"]

def notification(threshold):
    return {
        "Notification": {
            "NotificationType": "ACTUAL",
            "ComparisonOperator": "GREATER_THAN",
            "Threshold": threshold,
            "ThresholdType": "PERCENTAGE",
        },
        "Subscribers": [{"SubscriptionType": "SNS", "Address": topic_arn}],
    }

request = {
    "AccountId": account_id,
    "Budget": {
        "BudgetName": os.environ["BUDGET_NAME"],
        "BudgetLimit": {"Amount": amount, "Unit": "USD"},
        "TimeUnit": "MONTHLY",
        "BudgetType": "COST",
    },
    "NotificationsWithSubscribers": [
        notification(50),
        notification(90),
        notification(100),
    ],
}
with open(out_path, "w") as f:
    json.dump(request, f)
PYEOF

if command -v cygpath >/dev/null 2>&1; then
  BUDGET_REQ_FILE="$(cygpath -m "${BUDGET_REQ_FILE}")"
fi
aws budgets create-budget --cli-input-json "file://${BUDGET_REQ_FILE}"

echo "Budget ${BUDGET_NAME} created: \$${MONTHLY_BUDGET_USD}/month, alerts at 50/90/100% actual spend -> ${TOPIC_ARN}"
