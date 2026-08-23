"""FastAPI application entrypoint — Phase H (PLAN.md). Wires together the
4 endpoints (POST /query, GET /health, GET /sources/{doc_id}, POST
/documents/upload) over the already-built RAG pipeline
(app/rag/pipeline.py) and embeddings pipeline (app/embeddings/pipeline.py).

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

from app.api import documents, health, query, sources
from app.api.schema import apply_document_uploads_schema
from app.db.connection import get_connection
from app.db.schema import apply_schema

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Idempotent CREATE TABLE/INDEX IF NOT EXISTS for every table this
    API layer touches on startup: `chunks` (app/db/schema.py — already
    required by the RAG pipeline this API wraps) and `document_uploads`
    (app/api/schema.py — new this phase). Safe to run on every process
    start, same as every other apply_schema()-style function in this
    codebase (app/granicus/schema.py's apply_granicus_schema())."""
    conn = get_connection()
    try:
        apply_schema(conn)
        apply_document_uploads_schema(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="South St. Petersburg Infrastructure Intelligence API",
    description=(
        "RAG API over South St. Petersburg civic infrastructure records — "
        "local government meeting records, grants, and uploaded negotiation "
        "documents. See this repo's CLAUDE.md/DECISIONS.md for the full "
        "grounding-contract and provenance model behind these endpoints."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(query.router)
app.include_router(health.router)
app.include_router(sources.router)
app.include_router(documents.router)

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
