"""One-off migration script (DECISIONS #154): backfills `content_hash` for
every real, already-embedded row in `chunks` that predates this column,
then locks the column to NOT NULL once the backfill is confirmed complete.

Why this exists as a separate script, not folded into apply_schema()
itself (app/db/schema.py): apply_schema() only ever runs cheap, idempotent
DDL (CREATE TABLE/INDEX IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, a
conditional SET NOT NULL) — it is called on every process start and must
stay fast and side-effect-free against already-migrated data. Computing
and writing a real sha256 for ~356 real production rows is real, one-time
migration work; it belongs in an explicit, on-demand script run once
during rollout, not silently repeated (as a no-op, but still a table scan)
on every future process start.

Safe to re-run: rows that already have a content_hash are left untouched
(WHERE content_hash IS NULL) — a second run after the first succeeded is a
fast no-op that finds nothing to backfill.

Usage (matches this project's existing one-off-migration pattern,
scripts/run_embedding_pipeline.py / scripts/embed_granicus_transcripts.py):
    python scripts/backfill_content_hash.py

Against the real live RDS corpus (not the local dev DB), run this the same
way DECISIONS #124/#143 already established for one-off scripts that need
to reach RDS from inside the VPC — via the registered south-st-pete-migrate
ECS task definition (infra/run_migration_task.sh), e.g.:
    AWS_PROFILE=south-st-pete infra/run_migration_task.sh -- \\
        python scripts/backfill_content_hash.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import get_connection
from app.db.schema import apply_schema
from app.embeddings.pipeline import compute_content_hash

_SELECT_NULL_HASH_SQL = "SELECT chunk_id, chunk_text FROM chunks WHERE content_hash IS NULL"
# `AND content_hash IS NULL` (rag-review pre-commit finding, DECISIONS
# #154): this script reads its row list once, then updates row-by-row in
# a separate loop — a real, if narrow, window for a concurrent live
# recrawl (the weekly Saturday schedule, DECISIONS #126/#134) to commit a
# genuine content_hash for the SAME still-NULL legacy row in between this
# script's own SELECT and its UPDATE for that row. Without the guard, this
# script's later blind UPDATE would clobber the recrawl's fresh, correct
# hash with a stale one computed from the text this script read earlier —
# not silent corpus corruption (the mismatch self-heals: the next pass
# that touches this chunk_id sees the hash disagree with chunk_text and
# re-embeds for real), but a real wasted paid re-embed and a real,
# avoidable moment where content_hash doesn't match chunk_text. The guard
# makes this a no-op exactly when a concurrent writer already won.
_UPDATE_HASH_SQL = "UPDATE chunks SET content_hash = %s WHERE chunk_id = %s AND content_hash IS NULL"
_COUNT_NULL_HASH_SQL = "SELECT count(*) FROM chunks WHERE content_hash IS NULL"
_IS_NULLABLE_SQL = (
    "SELECT is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'chunks' AND column_name = 'content_hash'"
)


def main() -> None:
    conn = get_connection()

    # Adds the nullable content_hash column if this is the very first run
    # against this database (no-op if it already exists) — safe to call
    # before the backfill below has populated anything, since the lock-to-
    # NOT-NULL half of apply_schema() only fires once zero NULL rows remain
    # (app/db/schema.py's _LOCK_CONTENT_HASH_NOT_NULL_SQL).
    apply_schema(conn)

    with conn.cursor() as cur:
        cur.execute(_SELECT_NULL_HASH_SQL)
        rows = cur.fetchall()

    print(f"{len(rows)} row(s) found with content_hash IS NULL — backfilling.")

    backfilled = 0
    already_won_by_concurrent_writer = 0
    with conn.cursor() as cur:
        for chunk_id, chunk_text in rows:
            content_hash = compute_content_hash(chunk_text)
            cur.execute(_UPDATE_HASH_SQL, (content_hash, chunk_id))
            # rowcount is 0 exactly when the guard above (`AND content_hash
            # IS NULL`) found a concurrent writer already got there first —
            # a real, if rare, possibility during a live rollout, not an
            # error (see _UPDATE_HASH_SQL's comment).
            if cur.rowcount == 1:
                backfilled += 1
            else:
                already_won_by_concurrent_writer += 1
    conn.commit()
    print(f"Backfilled {backfilled} row(s).")
    if already_won_by_concurrent_writer:
        print(
            f"{already_won_by_concurrent_writer} row(s) already had a real "
            "content_hash set by a concurrent writer before this script's "
            "UPDATE reached them — left untouched, not overwritten."
        )

    # Re-run apply_schema() now that every row has a real content_hash —
    # this is what actually flips the column to NOT NULL for real (see
    # app/db/schema.py's _LOCK_CONTENT_HASH_NOT_NULL_SQL).
    apply_schema(conn)

    with conn.cursor() as cur:
        cur.execute(_COUNT_NULL_HASH_SQL)
        (remaining_null,) = cur.fetchone()
        cur.execute(_IS_NULLABLE_SQL)
        row = cur.fetchone()
        is_nullable = row[0] if row else "column not found"

    print(f"Remaining NULL content_hash rows: {remaining_null}")
    print(f"content_hash column is_nullable: {is_nullable}")

    if remaining_null == 0 and is_nullable == "NO":
        print("OK: backfill complete and content_hash is locked NOT NULL.")
    else:
        print(
            "WARNING: backfill did not fully complete — content_hash is "
            "NOT yet locked NOT NULL. Re-run this script once the "
            "remaining NULL rows are resolved."
        )

    conn.close()


if __name__ == "__main__":
    main()
