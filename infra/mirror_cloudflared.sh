#!/bin/bash
# Copies the pinned cloudflared image (infra/CLOUDFLARED_PIN) from Docker Hub
# into this account's private ECR repo `south-st-pete-app`, so a Docker Hub
# rate limit (anonymous pulls from shared AWS addresses) can't stop a task
# from starting (DECISIONS #178). The task definition then pulls it from ECR
# BY DIGEST. Run once per cloudflared version, as the deploy user, before
# register_app_taskdefs.sh.
#
# Usage:  AWS_PROFILE=south-st-pete bash infra/mirror_cloudflared.sh
# Requires: Docker running (with buildx), AWS_PROFILE set, ECR push rights
# (the deploy user already pushes app images).
#
# The rehearsal task (tunnel-check) reuses the production tunnel token. Once the
# hostname is live, a running rehearsal task would receive real requests with
# no api behind it: stop it right after the check (runbook step 0c).
#
# How: `docker buildx imagetools create` copies the whole multi-architecture
# image list registry to registry without unpacking it, so the digest should
# stay identical. That is expected but UNTESTED here; the script checks the
# digest ECR reports afterwards and refuses to call it done if it differs.
#
# The rehearsal task (tunnel-check) reuses the production tunnel token. Once
# the hostname is live, a running rehearsal task would answer real requests
# with no api behind it, so stop it right after the check (runbook step 0c).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_PROFILE:?Set AWS_PROFILE}"
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
export MSYS_NO_PATHCONV=1

VERSION="$(sed -n 's/^CLOUDFLARED_VERSION=//p' infra/CLOUDFLARED_PIN | tr -d '\r')"
DIGEST="$(sed -n 's/^CLOUDFLARED_DIGEST=//p' infra/CLOUDFLARED_PIN | tr -d '\r')"
printf '%s' "$DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' \
  || { echo "infra/CLOUDFLARED_PIN: bad CLOUDFLARED_DIGEST" >&2; exit 1; }
printf '%s' "$VERSION" | grep -Eq '^[0-9]{4}\.[0-9]+\.[0-9]+$' \
  || { echo "infra/CLOUDFLARED_PIN: bad CLOUDFLARED_VERSION" >&2; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"
TAG="cloudflared-${VERSION}"
SRC="docker.io/cloudflare/cloudflared@${DIGEST}"

# Pre-check before the external write: already mirrored with this digest?
# Only "image not found" means "not mirrored yet"; any other error (bad
# profile, throttling, access denied) stops the script instead of being read
# as "not mirrored".
set +e
ERR_FILE="$(mktemp)"
LOOKUP="$(aws ecr describe-images --repository-name south-st-pete-app \
  --image-ids "imageTag=${TAG}" --query 'imageDetails[0].imageDigest' \
  --output text 2>"${ERR_FILE}")"
STATUS=$?
LOOKUP_ERR="$(cat "${ERR_FILE}")"; rm -f "${ERR_FILE}"
set -e
if [ $STATUS -eq 0 ]; then
  if [ "$LOOKUP" = "$DIGEST" ]; then
    echo "Already mirrored: ${REGISTRY}/south-st-pete-app@${DIGEST}"; exit 0
  fi
  echo "ECR tag ${TAG} exists with a DIFFERENT digest (${LOOKUP}); refusing to overwrite." >&2; exit 1
elif ! printf '%s' "$LOOKUP_ERR" | grep -q "ImageNotFoundException"; then
  echo "Could not check ECR: ${LOOKUP_ERR} ${LOOKUP}" >&2; exit 1
fi

aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}"
docker buildx imagetools create --tag "${REGISTRY}/south-st-pete-app:${TAG}" "${SRC}"

GOT="$(aws ecr describe-images --repository-name south-st-pete-app \
  --image-ids "imageTag=${TAG}" --query 'imageDetails[0].imageDigest' --output text)"
if [ "$GOT" != "$DIGEST" ]; then
  echo "Mirrored, but ECR reports digest ${GOT}, not the pinned ${DIGEST}." >&2
  echo "Do NOT register task definitions yet. Either verify ${GOT} is the same image and put it in infra/CLOUDFLARED_PIN, or investigate." >&2
  exit 1
fi
# An ECR lifecycle policy that expires old or untagged images could delete this
# image later and stop the api from starting. Best effort: show what exists.
if aws ecr get-lifecycle-policy --repository-name south-st-pete-app >/dev/null 2>&1; then
  echo "WARNING: repo south-st-pete-app has a lifecycle policy. Confirm it cannot expire tag ${TAG} (aws ecr get-lifecycle-policy --repository-name south-st-pete-app)." >&2
fi
echo "Mirrored and verified: ${REGISTRY}/south-st-pete-app@${DIGEST}"
