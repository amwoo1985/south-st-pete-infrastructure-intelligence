"""One-off migration helper: loads a JSON export of already-completed
``granicus_transcription_jobs`` rows into whatever DB LOCAL_DB_* points
at. Exists because there is no network path from a Fargate task (which
can reach RDS) to a developer's local docker-compose Postgres (which
cannot be reached from inside the VPC) — the export file is the relay.

Does NOT re-run transcription (no Whisper API call here) — these rows
were already transcribed once, locally; this only copies the completed
text so scripts/embed_granicus_transcripts.py can then embed it against
the target DB without re-incurring per-minute transcription cost.

Idempotent via mp3_url's PRIMARY KEY (app/granicus/schema.py) — ON
CONFLICT DO NOTHING, per .claude/rules/data.md's idempotency-key rule.

Usage:
    python scripts/load_granicus_export.py <path-to-export.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import get_connection
from app.granicus.schema import GRANICUS_JOB_COLUMNS, apply_granicus_schema

_INSERT_SQL = f"""
INSERT INTO granicus_transcription_jobs ({", ".join(GRANICUS_JOB_COLUMNS)})
VALUES ({", ".join(f"%({col})s" for col in GRANICUS_JOB_COLUMNS)})
ON CONFLICT (mp3_url) DO NOTHING
"""


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/load_granicus_export.py <path-to-export.json>")
        sys.exit(1)

    rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(f"{len(rows)} row(s) in export file.")

    conn = get_connection()
    try:
        apply_granicus_schema(conn)

        inserted = 0
        with conn.cursor() as cur:
            for row in rows:
                missing = [col for col in GRANICUS_JOB_COLUMNS if col not in row]
                if missing:
                    raise ValueError(
                        f"Export row for {row.get('mp3_url', '<unknown mp3_url>')!r} "
                        f"is missing required column(s): {missing}"
                    )
                cur.execute(_INSERT_SQL, row)
                inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()

    print(f"Inserted {inserted} new row(s); {len(rows) - inserted} already present (skipped).")


if __name__ == "__main__":
    main()
