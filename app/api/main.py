"""FastAPI application entrypoint. Wires together the endpoints
(POST /query, GET /health, GET /sources/{doc_id}, POST /documents/upload, and the
organization/membership/support-grant routes in app/api/admin.py) over the already-built RAG
pipeline (app/rag/pipeline.py) and embeddings pipeline
(app/embeddings/pipeline.py).

Run locally (repo root, docker-compose `db` service up, `.env`
populated per app/db/connection.py):

    uvicorn app.api.main:app --reload

Then Swagger UI is at http://127.0.0.1:8000/docs.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import admin, documents, health, invitations, query, sources
from app.db.connection import get_connection
from app.db.migrate import assert_schema_current

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Refuses to start if the database schema is behind this build or an
    applied migration was edited (app/db/migrate.py, DECISIONS #160). The
    API never runs DDL: migrations are applied separately, as the
    table-owning role, before a deploy."""
    conn = get_connection()
    try:
        assert_schema_current(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="Coalition Grant App API",
    description=(
        "Grounded, source-cited retrieval over uploaded organization "
        "documents — the foundation of the coalition grant app (DECISIONS "
        "#158). See CLAUDE.md/DECISIONS.md for the grounding contract and "
        "provenance model behind these endpoints."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(query.router)
app.include_router(health.router)
app.include_router(sources.router)
app.include_router(documents.router)
app.include_router(admin.router)
app.include_router(invitations.router)

# Static UI (DECISIONS #105) — plain HTML/JS/CSS served by this same app,
# no separate container. Mounted at "/" LAST, after every API router
# above: Starlette matches routes in registration order, so /query,
# /health, /sources/{doc_id}, /documents/upload (and FastAPI's own
# /docs, /openapi.json, added at FastAPI() construction time, earlier
# still) all match their explicit routes first. This Mount only catches
# whatever's left — index.html at "/" (html=True serves it for any path
# under the mount with no exact file match, e.g. client-side routes),
# plus /style.css and /app.js as static files. Mounting this BEFORE the
# routers would let it shadow every API route with a 404, since a Mount
# matches its whole path prefix greedily regardless of what's under it.
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
