"""FastAPI application entrypoint — Phase H (PLAN.md). Wires together the
5 endpoints (POST /query, GET /health, GET /health/crawlers, GET
/sources/{doc_id}, POST /documents/upload) over the already-built RAG
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

from app.api import crawler_health, documents, health, query, sources
from app.api.schema import apply_document_uploads_schema
from app.db.connection import get_connection
from app.db.crawl_runs import apply_crawl_runs_schema
from app.db.schema import apply_schema
from app.granicus.schema import apply_granicus_schema

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Idempotent CREATE TABLE/INDEX IF NOT EXISTS for every table this
    API layer touches on startup: `chunks` (app/db/schema.py — already
    required by the RAG pipeline this API wraps), `document_uploads`
    (app/api/schema.py), `crawl_runs` (app/db/crawl_runs.py), and
    `granicus_transcription_jobs` (app/granicus/schema.py). The latter two
    were previously only ever created by one-off scripts
    (scripts/run_live_recrawl.py, scripts/register_granicus_meeting.py) —
    api-review pre-commit finding on GET /health/crawlers (this phase):
    without this, a genuinely fresh environment (new RDS instance, first
    boot before any crawler/Granicus script has run) would 500 on that
    endpoint instead of showing the never_run-for-everything state it
    exists to display, since the tables it reads wouldn't exist yet. Safe
    to call on every process start, same as `apply_schema`/
    `apply_document_uploads_schema` above — every one of these functions
    is CREATE TABLE/INDEX IF NOT EXISTS, not a migration."""
    conn = get_connection()
    try:
        apply_schema(conn)
        apply_document_uploads_schema(conn)
        apply_crawl_runs_schema(conn)
        apply_granicus_schema(conn)
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
app.include_router(crawler_health.router)
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
