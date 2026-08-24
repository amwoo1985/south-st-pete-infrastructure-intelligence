#!/bin/bash
# Builds and pushes infra/Dockerfile.migrate — the one-off, non-deployed
# image used to load/reload the RAG corpus into RDS (DECISIONS #124).
# Separate ECR repo from the real app image (south-st-pete-app) so this
# tooling's tags never appear alongside, or get confused with, what's
# actually running in ECS.
#
# Usage:
#   AWS_PROFILE=south-st-pete infra/build_push_migrate_image.sh
# Requires: AWS_PROFILE set, Docker running.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1

# Required: Dockerfile.migrate.dockerignore is a BuildKit-only feature
# (see infra/Dockerfile.migrate's header comment) — without it, a legacy
# build silently uses the root .dockerignore and COPY scripts/ hard-fails.
export DOCKER_BUILDKIT=1

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/south-st-pete-app-migrate"
TAG="$(git rev-parse --short HEAD)"
IMAGE="${REPO}:${TAG}"

aws ecr describe-repositories --repository-names south-st-pete-app-migrate >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name south-st-pete-app-migrate >/dev/null

aws ecr get-login-password | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

docker build -f infra/Dockerfile.migrate -t "${IMAGE}" .
docker push "${IMAGE}"

echo "${IMAGE}"
