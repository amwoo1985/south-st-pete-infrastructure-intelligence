"""Phase E step 7 validation: hand-picked REAL queries against the real
embedded slice in Postgres, using real cosine-distance search
(`embedding <=> query_vector`, the operator that matches the HNSW index's
vector_cosine_ops — see app/db/schema.py / DECISIONS #71).

This is NOT the retrieval layer (Phase F, not built yet — no relevance-
threshold-filtered, deduplicated, token-budgeted context assembly here).
It is a manual ranking-sanity check: does the nearest real chunk to each
query actually look like the right answer?
"""

from __future__ import annotations

import sys
from pathlib import Path

import openai
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db.connection import get_connection

load_dotenv()

QUERIES = [
    "What is the SHIP income limit for a household of 4?",
    "What did the Pinellas County commissioners decide about the destination "
    "marketing research agreement with Future Partners?",
    "When does the Senior Citizens Services Wellness grant distribute its awards?",
    "What is the plan for deep energy efficiency retrofits of municipal facilities?",
    # Deliberately off-corpus: nothing in the 91-chunk slice discusses Duke
    # Energy solar rebates — checks that an unrelated query doesn't rank
    # high/confident against unrelated content.
    "What rebate does Duke Energy offer homeowners for rooftop solar panels?",
    # New, post-run-all: targets SPHA program pages (DECISIONS #50/#65),
    # newly embedded in this run. Real content confirmed live in the
    # `chunks` table before writing this query — three SPHA sections
    # ("SPHA Public Housing — Paying Rent", "SPHA Affordable Housing —
    # Paying Rent", "Section 8 / HCV Program — Renting a Unit") mention
    # rent due dates/late fees; the top hit should be one of those two
    # near-duplicate "Paying Rent" sections, not the unrelated one.
    "When is SPHA public housing rent due each month, and what happens if it's late?",
    # New, post-run-all: targets a pinellas.gov HCD program page
    # (DECISIONS #53/#66), newly embedded in this run — specifically the
    # DECISIONS #67 contentless-heading forward-merge case
    # ("Eligible Improvements › Home Repair Loan Program").
    "What home repairs are eligible under Pinellas County's Home Repair Loan Program?",
]


def main() -> None:
    client = openai.OpenAI()
    conn = get_connection()
    cur = conn.cursor()

    for query in QUERIES:
        resp = client.embeddings.create(input=query, model="text-embedding-3-small")
        query_vector = resp.data[0].embedding

        cur.execute(
            "SELECT doc_type, section_label, chunk_text, "
            "1 - (embedding <=> %s::vector) AS similarity "
            "FROM chunks WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT 3",
            (query_vector, query_vector),
        )
        rows = cur.fetchall()

        print(f"\n=== Query: {query!r} ===")
        for doc_type, section_label, chunk_text, similarity in rows:
            snippet = chunk_text[:160].replace("\n", " ")
            print(f"  sim={similarity:.4f} [{doc_type}] {section_label!r}")
            print(f"    {snippet}")

    conn.close()


if __name__ == "__main__":
    main()
