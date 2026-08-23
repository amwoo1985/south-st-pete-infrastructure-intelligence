"""GET /sources/{doc_id} — doc_id IS chunk_id (decided already, per the
orchestrator's directive; not re-derived here). Returns a chunks-table
row minus its embedding vector (no reason to ship 1536 floats to a
client) — explicit column list, never SELECT * (.claude/rules/data.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_db_conn
from app.api.schemas import SourceRecordResponse
from app.db.schema import CHUNK_COLUMNS

router = APIRouter()

_SOURCE_COLUMNS = tuple(c for c in CHUNK_COLUMNS if c != "embedding")
_SELECT_SQL = f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM chunks WHERE chunk_id = %s"


@router.get("/sources/{doc_id}", response_model=SourceRecordResponse)
def get_source(doc_id: str, conn=Depends(get_db_conn)) -> SourceRecordResponse:
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (doc_id,))
        row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"No source found for doc_id={doc_id!r}")

    return SourceRecordResponse(**dict(zip(_SOURCE_COLUMNS, row)))
