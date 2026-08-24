# South St. Petersburg Infrastructure Intelligence — single application image.
# One image, config-driven per DECISIONS #8: this Dockerfile bakes in no role
# (api/worker). Role is selected entirely by docker-compose.yml's per-service
# `command:` — see docker-compose.yml's `api`/`worker` services.
#
# Base pinned to match .venv's confirmed-working interpreter exactly
# (`python --version` -> 3.12.10) — no `:latest` anywhere, per
# .claude/rules/deploy.md ("pin base image versions").
FROM python:3.12.10-slim

WORKDIR /app

# System dependency: ffmpeg, required at runtime by the worker service's
# pydub-based audio splitting (app/granicus/worker.py's duration-aware
# chunked transcription) — pydub shells out to the ffmpeg binary, it is
# not a pip package on its own. Installed before the pip-install layer,
# not after: apt packages change far less often than requirements.txt
# during active development, so this ordering keeps the apt layer cached
# across the requirements.txt edits that happen constantly, rather than
# invalidating a multi-hundred-MB pip layer on requirements.txt bumps
# and vice versa. Same base image serves both api/worker (DECISIONS
# #8's one-image, config-driven-role convention) and the already-live
# Fargate worker task, so this benefits both local docker-compose and
# the deployed worker, not just local dev.
# deploy-review note: not pinned to an exact apt package version
# (e.g. ffmpeg=5.1.9-0+deb12u1, the version this actually resolved to
# when last built) the way the base image tag above is pinned.
# Deliberate, not an oversight: Debian's security-updates repo purges
# superseded exact package versions, so a hard version pin here would
# make a future rebuild fail loud once that version rotates out —
# trading a small reproducibility gain for a real risk of breaking
# every future build of this image. Reproducibility instead comes from
# the pinned base image tag fixing the OS release/repo snapshot; the
# ffmpeg build within that snapshot may drift by a security patch
# level between rebuilds, which is accepted here.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (separate layer from app code) so a code-only
# change doesn't invalidate the pip-install layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole app/ tree — not a narrower subset — so any subdirectory
# added by a concurrently-working specialist (e.g. api-layer's static-assets
# dir under app/api/, or crawler's app/granicus/run_worker.py) is included
# without this file needing to change again.
COPY app/ app/

# No secrets, no baked role, no EXPOSE tied to one service — the api
# service's port mapping lives in docker-compose.yml. No CMD/ENTRYPOINT:
# every service in docker-compose.yml supplies its own `command:`.
