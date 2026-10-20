"""GET /sources/{doc_id} — doc_id IS chunk_id (decided already, per the
orchestrator's directive; not re-derived here). Returns a chunks-table
row minus its embedding vector (no reason to ship 1536 floats to a
client) — explicit column list, never SELECT * (.claude/rules/data.md).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_db_conn
from app.api.schemas import SourceRecordResponse
from app.db.schema import CHUNK_COLUMNS

logger = logging.getLogger("api.sources")

router = APIRouter()

_SOURCE_COLUMNS = tuple(c for c in CHUNK_COLUMNS if c != "embedding")
_SELECT_SQL = f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM chunks WHERE chunk_id = %s"


@router.get("/sources/{doc_id}", response_model=SourceRecordResponse)
def get_source(doc_id: str, conn=Depends(get_db_conn)) -> SourceRecordResponse:
    # api-review pre-commit finding (DECISIONS #114): this query previously
    # had no exception handling at all, unlike every other route in this
    # layer — a DB error fell through to Starlette's default handler,
    # which returns plain text, not this API's usual {"detail": ...} JSON
    # shape, and logs generically instead of through this module's logger.
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_SQL, (doc_id,))
            row = cur.fetchone()
    except Exception as exc:
        logger.error("sources lookup doc_id=%r: database error: %s", doc_id, exc)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        ) from exc

    if row is None:
        raise HTTPException(status_code=404, detail=f"No source found for doc_id={doc_id!r}")

    return SourceRecordResponse(**dict(zip(_SOURCE_COLUMNS, row)))
