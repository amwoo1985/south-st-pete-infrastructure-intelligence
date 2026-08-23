"""Chunks and embeds every `status='completed'` Granicus transcription
job (`granicus_transcription_jobs`, `app/granicus/worker.py`) that isn't
already represented in `chunks` — the missing link between a completed
transcription and a queryable corpus entry (DECISIONS #116).

Manual script, run on demand — matches this project's existing pattern
for turning parsed content into embedded chunks (see
scripts/run_embedding_pipeline.py; there is no automatic
transcribe-then-chunk trigger anywhere in this codebase, by the same
established convention). Idempotent: re-running after new meetings
complete only costs API calls for the newly-completed ones, since
embed_and_insert_chunk's chunk_id pre-check skips anything already in
`chunks` (DECISIONS #58).

Usage:
    python scripts/embed_granicus_transcripts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import openai

from app.chunking.granicus_transcript import chunk_granicus_transcript
from app.db.connection import get_connection
from app.db.schema import apply_schema
from app.embeddings.pipeline import embed_and_insert_chunks
from app.granicus.schema import apply_granicus_schema

_SELECT_COMPLETED_SQL = (
    "SELECT mp3_url, meeting_title, transcript_text, source_url, "
    "published_date, retrieval_timestamp "
    "FROM granicus_transcription_jobs WHERE status = 'completed' "
    "ORDER BY published_date"
)


def main() -> None:
    conn = get_connection()
    apply_schema(conn)
    apply_granicus_schema(conn)

    with conn.cursor() as cur:
        cur.execute(_SELECT_COMPLETED_SQL)
        rows = cur.fetchall()

    print(f"{len(rows)} completed transcription job(s) found.")

    all_chunks = []
    for mp3_url, meeting_title, transcript_text, source_url, published_date, retrieval_timestamp in rows:
        chunks = chunk_granicus_transcript(
            mp3_url=mp3_url,
            meeting_title=meeting_title,
            transcript_text=transcript_text,
            source_url=source_url,
            published_date=published_date,
            retrieval_timestamp=retrieval_timestamp,
        )
        print(f"  {meeting_title!r} ({published_date}): {len(chunks)} chunk(s)")
        all_chunks.extend(chunks)

    print(f"\nTotal chunks to embed/insert (already-embedded ones will be skipped): {len(all_chunks)}")

    client = openai.OpenAI()
    result = embed_and_insert_chunks(conn, client, all_chunks)
    conn.close()

    print(f"\nResult: inserted={result.inserted} skipped={result.skipped} failed={result.failed}")
    if result.failed_chunk_ids:
        print(f"Failed chunk_ids: {result.failed_chunk_ids}")


if __name__ == "__main__":
    main()
