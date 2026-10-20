#!/bin/bash
# Fills infra/{api,migrate}-taskdef.template.json placeholders from the
# repo-root .env (never committed — DECISIONS #69) and registers both
# task definitions for real. The api runs as the limited app_runtime DB
# role; only the one-off migrate task gets the RDS master credentials
# (DECISIONS #160). Templates hold ONLY placeholders
# (__PLACEHOLDER__ tokens), never real values — .claude/rules/deploy.md's
# "secrets never live in committed config" rule (DECISIONS #119/#120:
# plain environment variables are this project's chosen secrets path,
# since neither Secrets Manager nor SSM Parameter Store access exists on
# the deploy IAM user; the real-value task definition still ends up
# holding a plaintext secret once registered — see #120's disclosure).
#
# Usage:
#   IMAGE_TAG=<git-sha> infra/register_app_taskdefs.sh
# Requires: AWS_PROFILE set, python with python-dotenv (the repo .venv),
# .env present with RDS_HOST/RDS_MASTER_USERNAME/RDS_MASTER_PASSWORD/
# APP_RUNTIME_DB_PASSWORD/OPENAI_API_KEY/UPLOAD_API_KEY, docker image already pushed to ECR under
# the given tag.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE_TAG:?Set IMAGE_TAG to the git commit SHA of the pushed image (e.g. IMAGE_TAG=$(git rev-parse --short HEAD))}"
: "${AWS_PROFILE:?Set AWS_PROFILE}"

export AWS_DEFAULT_REGION=us-east-1
export MSYS_NO_PATHCONV=1

# Derived live, not hardcoded (deploy-review pre-commit finding, DECISIONS
# #121) — this script stays correct if it's ever run under a different
# AWS account/profile rather than silently pushing to the wrong registry.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IMAGE="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app:${IMAGE_TAG}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

for role in api migrate; do
  OUT_FILE="${TMP_DIR}/${role}-taskdef.json"
  # .env is parsed by python-dotenv (handles quoting and CRLF line endings),
  # not `source`d by the shell, and placeholders are replaced as literal JSON
  # string values, never via sed, so a secret containing & # \ " or a CR
  # can't be mangled or break the JSON (deploy-review finding on the old sed
  # path). Secrets travel via the .env file only, never argv. Every required
  # value is checked for presence and rejected if it's still a .env.example
  # placeholder (changeme*).
  python - "infra/${role}-taskdef.template.json" "${IMAGE}" "${ACCOUNT_ID}" > "${OUT_FILE}" <<'PY'
import json, sys
from dotenv import dotenv_values

template_path, image, account_id = sys.argv[1:4]
env = {k: (v or "").strip() for k, v in dotenv_values(".env", interpolate=False).items()}
required = ["RDS_HOST", "RDS_MASTER_USERNAME", "RDS_MASTER_PASSWORD", "APP_RUNTIME_DB_PASSWORD", "OPENAI_API_KEY", "UPLOAD_API_KEY"]
bad = [k for k in required if not env.get(k) or env[k].lower().startswith(("changeme", "sk-changeme"))]
if bad:
    sys.exit("Refusing to register: missing or still-placeholder in .env: " + ", ".join(bad))
values = {
    "__IMAGE__": image,
    "__ACCOUNT_ID__": account_id,
    "__DB_HOST__": env["RDS_HOST"],
    "__RDS_MASTER_USERNAME__": env["RDS_MASTER_USERNAME"],
    "__RDS_MASTER_PASSWORD__": env["RDS_MASTER_PASSWORD"],
    "__APP_RUNTIME_DB_PASSWORD__": env["APP_RUNTIME_DB_PASSWORD"],
    "__OPENAI_API_KEY__": env["OPENAI_API_KEY"],
    "__UPLOAD_API_KEY__": env["UPLOAD_API_KEY"],
}


def fill(node):
    if isinstance(node, dict):
        return {k: fill(v) for k, v in node.items()}
    if isinstance(node, list):
        return [fill(v) for v in node]
    if isinstance(node, str):
        for placeholder, value in values.items():
            node = node.replace(placeholder, value)
        return node
    return node


with open(template_path, encoding="utf-8") as f:
    filled = fill(json.load(f))
leftover = [p for p in values if p in json.dumps(filled)]
if leftover:
    sys.exit("Unfilled placeholders: " + ", ".join(leftover))
json.dump(filled, sys.stdout)
PY
  # aws.exe (Windows-native, e.g. Git Bash on Windows) can't resolve a POSIX
  # /tmp/... path in a --cli-input-json file:// URI. cygpath -m (drive letter
  # + forward slashes, e.g. C:/Users/...) converts it into a well-formed
  # file:// URI; -w's backslashes would not be. On non-Windows shells cygpath
  # doesn't exist, so fall back to the plain path.
  if command -v cygpath >/dev/null 2>&1; then
    OUT_FILE="$(cygpath -m "${OUT_FILE}")"
  fi
  aws ecs register-task-definition --cli-input-json "file://${OUT_FILE}" \
    --query 'taskDefinition.taskDefinitionArn' --output text
done
