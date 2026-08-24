#!/bin/bash
# Builds and pushes ../Dockerfile — the real, deployed application image
# (DECISIONS #8's one-image, config-driven-role convention: same image
# serves both the `api` and `worker` roles, role selected by each task
# definition's own `command:`/containerOverride, never by a separate build).
#
# Did not exist before DECISIONS #137: every prior build/push of this image
# (DECISIONS #120, #122) was a manual, undocumented `docker build`/`docker
# push` pair typed into a session rather than a reusable script — unlike
# infra/build_push_migrate_image.sh, which has had this exact shape since
# DECISIONS #124. That gap meant every real deploy of this image was, in
# practice, unrepeatable without re-deriving the right ECR repo name/tag
# scheme from old DECISIONS.md prose. This closes it, mirroring
# build_push_migrate_image.sh's structure (repo per DECISIONS #120: a
# separate ECR repo, `south-st-pete-app`, from the migrate tooling's
# `south-st-pete-app-migrate` — kept separate so the two never get confused
# about what's actually running in ECS).
#
# Usage:
#   AWS_PROFILE=south-st-pete infra/build_push_app_image.sh
# Requires: AWS_PROFILE set, Docker running.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1

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
