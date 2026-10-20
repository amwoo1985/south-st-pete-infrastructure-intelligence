# Coalition Grant App — single application image.
# One image, config-driven per DECISIONS #8: this Dockerfile bakes in no role.
# The command is supplied by docker-compose.yml / the ECS task definition.
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

# Copy the whole app/ tree so new subpackages are included without this
# file needing to change.
COPY app/ app/

# No secrets, no baked role, no EXPOSE tied to one service — the api
# service's port mapping lives in docker-compose.yml. No CMD/ENTRYPOINT:
# every runner supplies its own command.
