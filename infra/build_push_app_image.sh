#!/bin/bash
# Builds and pushes ../Dockerfile — the deployed application image
# (DECISIONS #8's one-image, config-driven-role convention: the role is
# selected by the task definition's own `command:`, never by a separate
# build). ECR repo: `south-st-pete-app` (DECISIONS #120).
#
# Usage:
#   AWS_PROFILE=south-st-pete infra/build_push_app_image.sh
# Requires: AWS_PROFILE set, Docker running.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1

# Dirty-tree guard (deploy-review finding, DECISIONS #137): this script tags
# the pushed image with the current commit SHA so anyone reading ECR later
# can trust "this tag == this commit's code." A dirty working tree breaks
# that promise silently — the built image would contain uncommitted changes
# under a tag that claims to be a specific, inspectable commit. Fail loud
# instead of tagging a lie.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing to build: working tree has uncommitted changes. Commit or stash first — the image tag below is a commit SHA and must match what it's built from." >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app"
TAG="$(git rev-parse --short HEAD)"
IMAGE="${REPO}:${TAG}"

aws ecr describe-repositories --repository-names south-st-pete-app >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name south-st-pete-app >/dev/null

aws ecr get-login-password | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

docker build -f Dockerfile -t "${IMAGE}" .
docker push "${IMAGE}"

echo "${IMAGE}"
