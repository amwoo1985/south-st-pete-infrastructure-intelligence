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
