# Finishing the Fargate deploy (Phase K)

Status per DECISIONS #120: image built and pushed, RDS pgvector-confirmed live. Task defs, security group, and ECS services are NOT yet created — blocked by Claude Code's own auto-mode safety guardrail on security-setting/infra-modifying commands, not by AWS/IAM permissions (those are unblocked, DECISIONS #119).

Run these yourself (`AWS_PROFILE=south-st-pete`, `AWS_DEFAULT_REGION=us-east-1`), or ask a session with looser Bash permissions to run them:

## 1. Register the real task definitions

```
IMAGE_TAG=$(git rev-parse --short HEAD) infra/register_app_taskdefs.sh
```

(Rebuild/push the image first if `HEAD` has moved since the last push — see the `docker build`/`docker push` commands in DECISIONS #120.)

## 2. Create a security group for public API access

```
SG_ID=$(aws ec2 create-security-group --group-name ssp-api-public-sg \
  --description "South St Pete Infra Intelligence - public inbound to the api service only" \
  --vpc-id vpc-06aac370e0afa8145 --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 8000 --cidr 0.0.0.0/0
```

Keep this scoped to port 8000 only, and keep it a *separate* security group from `ssp-fargate-sg` (sg-0b3761d661a400274, already whitelisted for RDS access, outbound-only) — the `api` task should get both attached; `worker` only needs `ssp-fargate-sg`.

## 3. Create the ECS services

```
aws ecs create-service --cluster south-st-pete-dev --service-name south-st-pete-api \
  --task-definition south-st-pete-api --desired-count 1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-055cdb6c9ae6cae23,subnet-0b5058ff93f41df1e,subnet-039cfd3ce1a7f9a9d],securityGroups=[sg-0b3761d661a400274,$SG_ID],assignPublicIp=ENABLED}"

aws ecs create-service --cluster south-st-pete-dev --service-name south-st-pete-worker \
  --task-definition south-st-pete-worker --desired-count 1 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-055cdb6c9ae6cae23,subnet-0b5058ff93f41df1e,subnet-039cfd3ce1a7f9a9d],securityGroups=[sg-0b3761d661a400274],assignPublicIp=ENABLED}"
```

## 4. Get the public URL and verify

```
aws ecs list-tasks --cluster south-st-pete-dev --service-name south-st-pete-api
# describe-tasks the returned task ARN, get its ENI, then describe-network-interfaces for the public IP
curl http://<public-ip>:8000/health
```

This IP is per-task and ephemeral — `assignPublicIp=ENABLED` gives the running task a public IP, not the service a stable address. It changes on every task replacement (deploy, crash, AZ rebalance). Fine for a one-off manual check; not a real "public URL" for ongoing use — an ALB (extra cost) or a DNS record updated on task-start would be the real fix if a stable URL is needed later.

## Status: done (DECISIONS #122-124)

Task defs registered, security group created, both ECS services live, RDS master password rotated, and the real corpus (342 Tier-1 chunks + 13 Granicus chunks) loaded into RDS — `/query` returns real grounded, cited answers against the live public IP. This runbook is kept for reference (re-deploys after an app-code change reuse steps 1/3/4; the old `pgvector-check`/password-rotation content below is historical).

To load newly-crawled or newly-transcribed content into RDS in the future (DECISIONS #124/#125):

```
AWS_PROFILE=south-st-pete infra/build_push_migrate_image.sh
AWS_PROFILE=south-st-pete IMAGE_TAG=$(git rev-parse --short HEAD) infra/register_migrate_taskdef.sh
AWS_PROFILE=south-st-pete infra/run_migration_task.sh -- python scripts/run_embedding_pipeline.py run-all
# Granicus: export completed jobs from local Postgres to infra/migration_data/ first (see
# scripts/load_granicus_export.py's docstring), then:
AWS_PROFILE=south-st-pete infra/run_migration_task.sh -- sh -c \
  "python scripts/load_granicus_export.py infra/migration_data/<export>.json && python scripts/embed_granicus_transcripts.py"
```

Deregister the `south-st-pete-migrate` task def revision after use — it carries the live RDS password as a plain env var (same pattern as `api`/`worker`, see the "Recurring, structural" note below).

**Recurring, structural, not fully closed:** every task definition registered under this project's plain-env-var secrets approach (no Secrets Manager/SSM access on this IAM user) carries whatever RDS password was live at registration time, readable indefinitely from a deregistered revision by anyone with `ecs:DescribeTaskDefinition`. Deregistering a task def after use only stops it from being launched again — it does not remove the secret AWS retains in that revision. Rotating the password (as done in #124) invalidates what a stale revision exposes, but the next task def registered will expose whatever is live *then*. Accepted per #120's original reasoning; revisit only if Secrets Manager/SSM access is ever added to this IAM user.

## Prepared, not yet live: weekly recrawl schedule + cost alerts

Both blocked on IAM permission gaps confirmed live this round (`south-st-pete-infra-dev` has zero EventBridge Scheduler permissions and zero `sns:CreateTopic`/`budgets:ViewBudget`) — real, tested-as-far-as-possible scripts exist and are ready to run the moment Amber authorizes the policy attachments named in each script's own header comment.

- `infra/register_recrawl_schedule.sh` — creates a scoped IAM execution role plus an EventBridge Scheduler schedule (`cron(0 6 ? * SAT *)`, every Saturday 06:00 UTC) that runs `python scripts/run_live_recrawl.py run` via `ecs:RunTask` against the existing `south-st-pete-migrate` task definition/image — reused deliberately rather than a new task family (see the script's own header for why). Neither the role creation nor the schedule creation was run this session; see the script header and this round's deploy-infra report for the reasoning on why the role creation was also left to Amber's sign-off even though it's independently unblocked. **Note this contradicts the "deregister the `south-st-pete-migrate` task def revision after use" convention stated above:** a recurring schedule needs that task def to stay ACTIVE indefinitely, which is a real change to this family's exposure model (not a new risk category — `api`/`worker` already run permanently-registered task defs with the same plain-env-var password, per #122 above — but a real extension of it to a third family) that has not yet had its own DECISIONS.md entry or Amber's sign-off.
- `infra/register_cost_alerts.sh` — creates an SNS topic + email subscription and an AWS Budgets monthly cost budget (50/90/100% actual-spend thresholds) wired to it. Needs `BUDGET_ALERT_EMAIL` and `MONTHLY_BUDGET_USD` set in `.env` first (see `.env.example`) — the dollar figure is deliberately not defaulted; see this round's deploy-infra report for 2-3 reasoned candidates awaiting Amber's pick.
