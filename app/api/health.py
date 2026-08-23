"""GET /health — a real dependency check (SELECT 1 against Postgres), not
a bare "the process is up" ping.

Deliberately does NOT also make a live OpenAI call on every hit (api-
layer design decision, this round, per the orchestrator's directive):
this endpoint is meant to be hit frequently — container health checks,
uptime monitors — and a live external API call on every poll adds real
latency and (at high enough poll frequency) real cost, for a signal that
POST /query's own error handling (app/api/query.py's 429/502/504/500
mapping) already surfaces directly and immediately the moment an actual
OpenAI-related failure happens on a real query. A green /health next to a
broken OpenAI key would only be wrong for the gap between the key
breaking and the next real /query call — for a solo-operator tool polled
at normal health-check cadence (seconds to minutes), that gap costs far
less than paying an OpenAI round-trip on every single poll would.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from app.api.schemas import HealthResponse
from app.db.connection import get_connection

logger = logging.getLogger("api.health")

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return HealthResponse(status="ok", db="ok")
    except Exception as exc:
        # Never leak the raw exception into the response body — log full
        # detail server-side, return the generic degraded shape.
        logger.error("health check: database unreachable: %s", exc)
        # Set a real non-200 status alongside the JSON body, not just
        # "degraded" in the payload with a 200 wrapper: infra tooling
        # (Fargate/ECS task health checks, uptime monitors) commonly keys
        # off HTTP status rather than parsing a JSON body, so this is
        # strictly more useful for near-zero extra cost. Flagged as an
        # api-layer decision not explicitly specified by this round's
        # brief — deploy-infra should confirm 503 is what its health
        # check config expects before this is relied on for container
        # restarts.
        response.status_code = 503
        return HealthResponse(status="degraded", db="error")
