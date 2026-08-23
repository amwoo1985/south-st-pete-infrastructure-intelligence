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

## Known open item

The `pgvector-check` task definition (DECISIONS #120) was registered with the real RDS master password as a plain env var before it was deregistered — AWS retains that revision indefinitely, readable by anyone with `ecs:DescribeTaskDefinition` in this account. **Consider rotating the RDS master password** before or shortly after finishing this deploy.
