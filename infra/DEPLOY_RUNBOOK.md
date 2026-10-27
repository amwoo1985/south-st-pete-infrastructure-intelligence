# Deploy runbook (Phase B release)

For whoever is running this next, with no one to ask. Do the steps in order. Step 0 needs the admin login; every other step uses the deploy user. Read `.claude/rules/secrets.md` first: never print `.env` values, never put a secret in a command argument.

## Where production is today

- The `south-st-pete-api` service runs task definition `:7`, image `1c3a804` (the Phase A build, DECISIONS #161/#167), against the encrypted RDS instance `applied-db`.
- The database schema is at migration **0003**. This release applies **0004** (users), **0005** (organizations, memberships, support access, audit log) and **0006** (invitations).
- This release also adds the Cloudflare Tunnel (DECISIONS #177, #178): `https://app.getapplied.org` reaches the api through a `cloudflared` container in the api task, and step 5b then closes the api's inbound firewall. The tunnel gives HTTPS and a stable hostname, but the browser sign-in pages are still B5, so nobody can sign in from a browser yet (DECISIONS #162), and so after this release the deployed instance cannot upload or query. That is expected (DECISIONS #169). No real partner documents go on it until Phase C.

## Before you start

- `.env` holds `RDS_HOST`, `RDS_MASTER_USERNAME`, `RDS_MASTER_PASSWORD`, `APP_RUNTIME_DB_PASSWORD`, `OPENAI_API_KEY` and the three `COGNITO_*` values (`infra/register_cognito.sh` prints the Cognito ones). After step 0b it also holds `CLOUDFLARE_TUNNEL_SECRET_ARN` (an identifier, not a secret) and must **not** hold a `CLOUDFLARE_TUNNEL_TOKEN` line.
- The git working tree is clean and on the commit you intend to ship (the build script refuses a dirty tree).
- Docker is running. `aws` and a working `python` are on the PATH (in Git Bash on Windows the repo's `.venv` python is fine).
- Two identities. **Deploy commands** run as the deploy user: `export AWS_PROFILE=south-st-pete`. It has no IAM rights. **Step 0** runs as the admin IAM user (console only, MFA, no access keys, DECISIONS #163): open AWS CloudShell from that console session, or use the console. Do not use `AWS_PROFILE=south-st-pete` for step 0.
- On Linux or macOS the scripts are not all executable: write `bash infra/<script>.sh` as shown.
- Record the starting state (read-only): `bash infra/run_migration.sh status` and `bash infra/run_migration.sh report`. Until step 2 these run on the old migrate task definition, so `status` shows 0001-0003 applied and says nothing about 0004-0006.

## Step 0: IAM (admin login in CloudShell or the console, once, before step 2)

**Done 2026-10-27 (DECISIONS #176).** The role and its `invite-people` policy exist. The PassRole command was **not needed**: per `simulate-principal-policy`, the deploy user may already pass this role when the receiving service is `ecs-tasks.amazonaws.com` (`allowed`, via `AmazonECS_FullAccess`). `infra/deploy-user-passrole-policy.json` stays only as a fallback if that policy is ever detached. The steps below are kept for rebuilding the account.

The api now needs its own narrow role so it can create invited people in Cognito. Three templates hold the exact policies, with `__ACCOUNT_ID__` and `__USER_POOL_ID__` placeholders:

- `infra/api-task-role-trust-policy.json` (who may use the role: ECS tasks in this account)
- `infra/api-task-role-policy.json` (what it may do: `cognito-idp:AdminCreateUser` and `cognito-idp:ListUsers` on this one user pool, nothing else)
- `infra/deploy-user-passrole-policy.json` (lets the deploy user attach the role to a task definition)

1. Fill the placeholders into **copies outside the repo** (in CloudShell, paste the three files and edit them there). The account id is `aws sts get-caller-identity --query Account --output text`; the pool id is `COGNITO_USER_POOL_ID` in `.env`.
2. Check what the deploy user already has: `aws iam list-user-policies --user-name south-st-pete-infra-dev` and `aws iam list-attached-user-policies --user-name south-st-pete-infra-dev`. If a broader `iam:PassRole` already exists, skip the third command below.
3. Create the role and attach the policies:

```
aws iam create-role --role-name south-st-pete-api-task-role --assume-role-policy-document file://<filled trust policy>
aws iam put-role-policy --role-name south-st-pete-api-task-role --policy-name invite-people --policy-document file://<filled role policy>
aws iam put-user-policy --user-name south-st-pete-infra-dev --policy-name pass-api-task-role --policy-document file://<filled passrole policy>
```

The role must exist **before step 2** (registering the task definitions): the api task definition names it, and what happens if it doesn't exist yet was not tested.

## Step 0b: the tunnel token and its secret (admin login; once, before step 1)

Why: `https://app.getapplied.org` reaches the api through a Cloudflare Tunnel (DECISIONS #177, #178). The `cloudflared` container in the api task needs the tunnel's token. The token is a password for the tunnel: **anyone holding it can run a connector for `app.getapplied.org`**. It goes into AWS Secrets Manager and nowhere else: not `.env`, not a command argument, not a chat, not a ticket.

The token that existed before this step (it sat in `.env` and passed through a browser extension, #177) counts as exposed. Replace it.

1. **Browser extension off, no agent driving the tab.** You do the clicks and the paste yourself. Do not use a password manager or clipboard-history tool for the token.
2. **Cloudflare dashboard** (Networking, Tunnels, `applied-api`): refresh the token. Cloudflare's wording is "Refresh token" or "Rotate token". Per Cloudflare's docs (not tested live), after this the old token cannot open *new* connections, but connectors already running with it **stay up until restarted**, so also delete the tunnel's existing connections (Cloudflare API: `DELETE /accounts/<account id>/cfd_tunnel/<tunnel id>/connections`, needs an API token with Cloudflare Tunnel Write). **Do not go on to step 3 until** `curl -sS -o /dev/null -w '%{http_code}\n' https://app.getapplied.org/health` prints **530** (no connector). That check is the real proof the old connection is gone. The dashboard shows the new token inside the "Add a replica" install command (the `eyJ...` string; **do not run that command**).
3. **AWS console as the admin user**, Secrets Manager, Store a new secret, "Other type of secret", the **Plaintext** tab (not key/value): paste the bare token. Name it exactly `applied/cloudflare-tunnel-token`. Leave the default encryption key (`aws/secretsmanager`). If you use a customer-managed key instead, the execution role also needs `kms:Decrypt` on it. On the rotation screen choose **Disable automatic rotation**. (Creating it from the command line is **untested**; use the console.)
4. Read the secret's ARN (not secret): `aws secretsmanager describe-secret --secret-id applied/cloudflare-tunnel-token --query ARN --output text`. It ends in a dash and six characters.
5. Fill `infra/tunnel-token-policy.json` into a copy **outside the repo** (in CloudShell: paste the file, then `sed -i "s#__TUNNEL_TOKEN_SECRET_ARN__#<the ARN>#" read-tunnel-token.json`, and check no `__` is left in it), and attach it to the **execution** role (never the task role: the api container must not be able to read the token): `aws iam put-role-policy --role-name south-st-pete-ecs-task-execution-role --policy-name read-tunnel-token --policy-document file://<filled copy>`. Read it back: `aws iam get-role-policy --role-name south-st-pete-ecs-task-execution-role --policy-name read-tunnel-token`, and confirm the `Resource` is the real ARN (IAM accepts a leftover placeholder as a resource that simply never matches, and the api task would then fail to start).
6. Check the deploy user may hand that role to ECS (#163 claimed this; #176 only checked the task role). `aws iam simulate-principal-policy --policy-source-arn <deploy user ARN> --action-names iam:PassRole --resource-arns arn:aws:iam::<account id>:role/south-st-pete-ecs-task-execution-role --context-entries ContextKeyName=iam:PassedToService,ContextKeyType=string,ContextKeyValues=ecs-tasks.amazonaws.com` should say `allowed`. If it does not, fill `infra/deploy-user-passrole-policy.json` for the execution role instead of the task role and attach it (as in Step 0). Registering the task definitions (step 2, which now also registers the cloudflared rehearsal definition) is the first live use of this permission; #176 only checked the task role.
7. **Local `.env`:** delete any `CLOUDFLARE_TUNNEL_TOKEN` line (also commented-out ones and old copies in backups, shell history and scratch files), and add `CLOUDFLARE_TUNNEL_SECRET_ARN=<the ARN from 4>`. `infra/register_app_taskdefs.sh` refuses to run while a token line is still there (it reports the line number, never the line, and never prints the value). Step 0b runs in AWS (steps 3 to 6, admin login) and on your own machine (this step): copy the ARN from the first to the second by hand.
8. Write down, for the record (DECISIONS): the ARN, the time you did step 2, and the policy readback from step 5.

If the secret is ever deleted and recreated its ARN changes: update `CLOUDFLARE_TUNNEL_SECRET_ARN` in `.env` and the `Resource` in the role policy, then re-register and redeploy. To change only the token, store a new value in the same secret (the console's "Retrieve secret value, Edit"), then `aws ecs update-service --cluster south-st-pete-dev --service south-st-pete-api --force-new-deployment`: a running container only reads the secret when it starts.

## Step 0c: put the cloudflared image in your own registry (deploy user, once per cloudflared version)

The task pulls cloudflared from this account's private ECR by digest, so a Docker Hub rate limit can't stop a task from starting. `export AWS_PROFILE=south-st-pete`, Docker running (with buildx), then `bash infra/mirror_cloudflared.sh`. Needs ECR push rights for the deploy user (it already pushes the app image; the pull-through-copy itself has not been run yet). It copies the image named in `infra/CLOUDFLARED_PIN`, checks that ECR reports the same digest, and refuses to overwrite a tag that has a different digest. If ECR reports a different digest it stops: do not register task definitions until that is understood. It also warns if the repository has a lifecycle policy: make sure the policy cannot expire the `cloudflared-<version>` tag, or the api would stop starting some day. `register_app_taskdefs.sh` refuses to run if the digest is not in ECR.

## Steps 1 to 6: ship the release (deploy user)

1. `bash infra/build_push_app_image.sh` builds and pushes the image and prints the tag (the git short SHA).
2. `IMAGE_TAG=<sha> bash infra/register_app_taskdefs.sh` registers the `api` and `migrate` task definitions. It refuses if any required `.env` value (including `COGNITO_*` and `CLOUDFLARE_TUNNEL_SECRET_ARN`) is missing or still a placeholder, if `.env` still has a `CLOUDFLARE_TUNNEL_TOKEN` line, or if the pinned cloudflared image is not in ECR (step 0c). It registers three definitions: `api` (now with the cloudflared container), `migrate`, and `tunnel-check` (step 2b). The api definition now names `south-st-pete-api-task-role`; the migrate one does not.
**2b. Rehearse the tunnel before the migration (still reversible).** `register_app_taskdefs.sh` also registered `south-st-pete-tunnel-check`: the cloudflared container alone, with the same pinned image and the same secret. Running it once proves the token, the secret's ARN, the execution role's permission (and its PassRole), the platform and the outbound connection to Cloudflare, **without touching the database and without any api behind the hostname**. It does **not** test the real task's start order, health check, shared memory or the essential-container behavior; the firewall staying open until step 5 is what bounds that risk (DECISIONS #178).

The deploy user's rights for `ecs:RunTask`, `ecr:DescribeImages` and `iam:PassRole` on the execution role are **not verified** (step 0b.6 simulates the last one); a refusal here means the admin login runs the command instead.

Precondition: `curl -sS -o /dev/null -w '%{http_code}\n' https://app.getapplied.org/health` prints **530**. Then:

```
NETWORK="$(aws ecs describe-services --cluster south-st-pete-dev --services south-st-pete-api --query 'services[0].networkConfiguration' --output json)"
TASK_ARN="$(aws ecs run-task --cluster south-st-pete-dev --task-definition south-st-pete-tunnel-check --launch-type FARGATE --network-configuration "$NETWORK" --query 'tasks[0].taskArn' --output text)"
aws ecs wait tasks-running --cluster south-st-pete-dev --tasks "$TASK_ARN"
```

Wait two minutes, then **all four** must hold:

1. `aws ecs describe-tasks --cluster south-st-pete-dev --tasks "$TASK_ARN" --query 'tasks[0].[lastStatus,stoppedReason]' --output text` shows `RUNNING` and `None` for the stop reason (`None` means no stop reason).
2. CloudWatch log group `/ecs/south-st-pete-app`, stream prefix `cloudflared-check`, shows registered tunnel connections.
3. `curl -sS -o /dev/null -w '%{http_code}\n' https://app.getapplied.org/health` no longer prints 530 (expected: **502**, a connector with no api behind it; the exact code is not verified, anything but 530 means connected).
4. **Token check, without printing anything:** `aws logs filter-log-events --log-group-name /ecs/south-st-pete-app --log-stream-name-prefix cloudflared-check --filter-pattern eyJ --query 'length(events)'` must print **0** (`eyJ` is how a token starts; never print the events themselves). If it is not 0, the token leaked into the logs: rotate it again (step 0b), and delete those log streams (or set the group's retention to a day) so the old value does not sit there. Whether cloudflared ever logs the token is unverified; this is the check.

Then **stop the rehearsal task at once** (it reuses the live token and would answer real requests with nothing behind them): `aws ecs stop-task --cluster south-st-pete-dev --task "$TASK_ARN"`, and confirm the hostname prints 530 again (retry for about a minute; the edge can lag). If `wait tasks-running` errors, the task stopped: read `stoppedReason` from `describe-tasks` (and stop the task if it is somehow still running). If any check fails: fix (usually the secret ARN in `.env`, the role policy, or a wrong token), re-register, and run again. Nothing irreversible has happened yet. Record the task definition revisions for DECISIONS.

3. **Take a snapshot first, then migrate.** `up` cannot be undone (see "If something goes wrong"). The database also has 7-day automated backups (DECISIONS #167), but a named snapshot is easier to find:
   ```
   aws rds create-db-snapshot --db-instance-identifier applied-db --db-snapshot-identifier applied-pre-phase-b-<date>
   aws rds wait db-snapshot-available --db-snapshot-identifier applied-pre-phase-b-<date>
   bash infra/run_migration.sh status
   bash infra/run_migration.sh up
   ```
   `up` applies 0004, 0005 and 0006 as the database owner and prints the log. If it fails with a lock timeout, the live api held a lock: wait a minute and run it again.
4. Update the service, then wait for it to settle:
   ```
   aws ecs update-service --cluster south-st-pete-dev --service south-st-pete-api --task-definition south-st-pete-api:<new revision>
   aws ecs wait services-stable --cluster south-st-pete-dev --services south-st-pete-api
   ```
   The new api task has two containers. If it never becomes stable, read the `cloudflared` log stream first (bad secret ARN or role policy are the usual causes). The old task keeps running meanwhile, and the firewall is still open.
5. Check through the tunnel. Do not start until `aws ecs wait services-stable` has returned **and** the new task shows RUNNING with both containers (`south-st-pete-api` and `cloudflared`):
   - `curl -sS https://app.getapplied.org/health` answers `{"status":"ok","db":"ok"}`.
   - `curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://app.getapplied.org/query -H 'content-type: application/json' -d '{"question":"x"}'` prints 401 (no token). Any other code, including a Cloudflare challenge page, is a failure to investigate (how Cloudflare treats curl here is unverified).
   - CloudWatch log group `/ecs/south-st-pete-app` has streams with prefix `api` (started, no `MigrationError`) and `cloudflared` (registered connections, **no `eyJ` text**).
   - The firewall is still open at this point, so if the sidecar misbehaves the api is still reachable at its task IP (find it through the task's network interface; it changes on every restart) and nothing is lost.

   You cannot test an invitation on the deployed instance until browser sign-in exists. If the task role is wrong, the first real invitation fails with a 502 and the api log shows `cognito ListUsers failed: AccessDeniedException` (or `cognito AdminCreateUser failed: ...`). Fix the role policy in IAM; new credentials reach the task within minutes, no redeploy.

   **Until the old civic-records corpus is removed from RDS (DECISIONS #158), the sign-in requirement (401) is the only thing between the internet and that corpus.** Do not weaken it.

5b. **Close the api's inbound firewall** (only after every check in step 5 has passed in the same sitting). Needs EC2 rights: the deploy user's are unverified, so use the admin login if it is refused.
   1. Find the security group: `aws ecs describe-services --cluster south-st-pete-dev --services south-st-pete-api --query 'services[0].networkConfiguration'`. Do not guess the id.
   2. Save the "before" **somewhere durable outside the repo and not /tmp** (Amber's private storage; below it is called `<keep>`): `aws ec2 describe-security-groups --group-ids <sg> --output json > <keep>/sg-before.json`, and the inbound rules alone, which is the file the revoke and the rollback both use: `aws ec2 describe-security-groups --group-ids <sg> --query 'SecurityGroups[0].IpPermissions' --output json > <keep>/sg-ingress.json`.
   3. Read it. Expected: exactly one inbound rule, tcp 8000 from `0.0.0.0/0`. That is inferred from old history (old deploy history in DECISIONS.md), not known. If it has anything else (more sources, `::/0`, other ports), stop and decide with Amber what to revoke.
   4. Check what else uses this group: `aws ec2 describe-network-interfaces --filters Name=group-id,Values=<sg> --query 'NetworkInterfaces[].Description'`, and `aws ec2 describe-security-groups --filters Name=ip-permission.group-id,Values=<sg>` for rules in other groups (RDS) that name it as a source. Revoking *inbound* rules of this group does not affect those, but write down what you see. Egress must stay open (`IpPermissionsEgress` allows all): cloudflared dials out on 7844.
   5. **Positive control:** `curl -m 5 http://<task public IP>:8000/health` must answer now. If it does not, your timeout test below proves nothing.
   6. Revoke exactly what 5b.2 saved: `aws ec2 revoke-security-group-ingress --group-id <sg> --ip-permissions file://<keep>/sg-ingress.json`.
   7. Verify (the "step 5" checks below are the main step 5): `aws ec2 describe-security-groups --group-ids <sg> --query 'SecurityGroups[0].IpPermissions'` prints `[]`; `curl -m 5 http://<task public IP>:8000/health` now times out; `curl -sS https://app.getapplied.org/health` still answers ok.

6. Seat Amber and Erica, below.

## Step 6: seat Amber and Erica (deploy user)

The tool is `bash infra/run_admin.sh` (DECISIONS #174). Run `export AWS_PROFILE=south-st-pete` first. Every command is a dry run until you add `--apply`: run it once without `--apply` and read the output, then again with it. A dry run rolls back, so any id it prints (an organization, for instance) does not exist; **take ids only from an `--apply` run**.

The people's accounts must exist in Cognito already; `--cognito-sub` is the `sub` shown in the Cognito console (User pools, the pool, Users, the person). Amber's account exists already.

```
bash infra/run_admin.sh create-user --cognito-sub <Amber's sub> --email <Amber's email> --platform-admin --reason "Vendor admin" --apply
```

Create Erica's account in the Cognito console (Create user, email, send the invitation), copy her `sub`, then:

```
bash infra/run_admin.sh create-user --cognito-sub <Erica's sub> --email <Erica's email> --reason "FLFPC administrator" --apply
bash infra/run_admin.sh create-org --legal-name "Florida Food Policy Council" --as <Amber's email> --reason "First customer" --apply
bash infra/run_admin.sh grant-org-admin --org <org id from the create-org --apply output> --user <Erica's email> --as <Amber's email> --reason "FLFPC administrator" --apply
```

Amber is a platform admin and Erica is **not**; a person who needs both uses two logins (DECISIONS #166). From here Erica invites her own staff through the web API. The tool is part of the image from step 1.

## If something goes wrong

- **Before step 3's `up`:** nothing has changed in the database. The old api (`:7`) keeps serving. Fix and retry.
- **After `up` you cannot go back to `:7`.** That build only knows migrations up to 0003 and refuses to start on a database that has 0004 or later ("Database has migration 0004 recorded but no such file exists"). Pointing the service back at `:7` is only safe if you have NOT run `up`. The migrations are additive, so the way out is **forward**: fix the new build and redeploy.
- **If the new revision fails to start:** the service is configured with a minimum healthy percent of 100, a maximum of 200 and the deployment circuit breaker **off** (read from the live service on 2026-10-21). From that configuration, expect the old task to keep running while ECS retries the new one in a loop until you fix it or change the service; that was inferred from the settings, not tested. A running old task keeps working, but if it restarts for any reason after `up`, it will not come back.
- Restoring the database from the step 3 snapshot is a last resort and loses everything written since; it needs Amber's decision.
- **`run_admin.sh` says `No module named app.admin`:** the migrate task definition still points at an older image; redo steps 1 and 2.
- **`refused: ...`** from the tool is a rule doing its job; the message says which. The tool never changes anything without `--apply`.

## Rolling back the tunnel (DECISIONS #178)

The tunnel changes how people reach the api, not the database, so its rollback is separate from the migration rollback above. Order matters: **restore the firewall first, then verify, then touch the task definition.**

- **Before step 5b (firewall still open):** nothing about exposure has changed. If cloudflared is the problem, register without it: `TUNNEL_SIDECAR=off IMAGE_TAG=<same sha> bash infra/register_app_taskdefs.sh` (it prints `NO TUNNEL SIDECAR` and registers only api and migrate), then `update-service` to that new api revision. Same Phase B image; the api is reachable at its task IP exactly as before.
- **After step 5b:** (1) re-add the saved rules: `aws ec2 authorize-security-group-ingress --group-id <sg> --ip-permissions file://<keep>/sg-ingress.json` (the same file the revoke used); (2) verify `curl -m 5 http://<task public IP>:8000/health` answers; (3) only then, if needed, register with `TUNNEL_SIDECAR=off` and update the service as above. Leaving `TUNNEL_SIDECAR` unset on any later deploy puts the sidecar back (and then needs `CLOUDFLARE_TUNNEL_SECRET_ARN`).
- **Cut public access without touching AWS:** in Cloudflare, delete the public hostname (immediate), or refresh the tunnel token. A refresh leaves the secret in Secrets Manager holding the *old* token, so any replacement task cannot connect until the secret is updated (step 0b); whether a refresh also drops live connectors is not verified, hence the connections-delete call in step 0b. Because cloudflared is essential, the task will probably exit and be replaced repeatedly (not verified: cloudflared might keep retrying instead); follow with the two bullets above or set the service's desired count to 0.
- **Never** point the service back at task definition `:7` once step 3's `up` has run (see above).
- A Cloudflare outage takes the app down; the container health check makes ECS replace the task while Cloudflare is unreachable, which does not help but does no harm.
- If a *later* deploy forgets the security-group state: the group stays closed on purpose. The tunnel is the way in.

## What this does not cover

The browser sign-in pages and the Cognito callback URLs for `https://app.getapplied.org` (B5; HTTPS and the hostname now exist through the tunnel, and the callback update must send the whole app-client configuration back because the update call resets omitted settings, DECISIONS #162), per-organization document walls (Phase C), and moving Cognito email to SES. The invitation email path is capped at 30 invitations a day across all customers because Cognito's built-in sender allows 50 emails a day for the whole AWS account; revisit that (count only invitations that send an email, or move to SES) before onboarding more tenants (DECISIONS #172, #175).
