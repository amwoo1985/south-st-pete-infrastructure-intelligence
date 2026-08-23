# Demo script

No video recording exists yet — this is the script insurance against live-deploy flakiness names (PLAN.md Phase L), written so a walkthrough can be run live or recorded later without re-deriving the beats.

## 1. Grounded answer with citations

Ask: **"What has Duke Energy committed to as part of the South St. Petersburg CBA negotiations?"**

Expect: an answer citing `st_petes_commitment.php` content, `not_in_corpus: false`, at least one citation with a real `source_url` and `published_date`.

## 2. Honest "not in corpus"

Ask something genuinely outside the indexed corpus — e.g. **"What did residents say during public comment about the Duke Energy franchise agreement?"** before any relevant Granicus meeting is transcribed.

Expect: `not_in_corpus: true`, a plain "not covered" response — not a hallucinated answer from the model's general knowledge of Duke Energy disputes elsewhere. This is the grounding contract's core promise, and the most important thing to show working.

## 3. Live upload, queryable immediately

Upload a short PDF or TXT file with one fact not present anywhere else in the corpus. Ask a question naming that fact.

Expect: `202` on upload, then a correct grounded answer with a citation whose `source_url` is `upload://<sha256>` and `doc_type` is `uploaded_document` — proving the upload path goes through the identical grounding contract as crawled content.

## 4. Real transcribed audio, cited

Ask: **"What did the Budget, Finance and Taxation Committee discuss on April 9th regarding the Community Redevelopment Agency external audit?"**

Expect: a correct answer naming the real external auditor and findings, with citations whose `doc_type` is `granicus_transcript` — proving the full Granicus pipeline (registration → transcription → chunking → embedding → retrieval) works end to end on real audio (DECISIONS #116, #117).

## 5. Known limitation, shown not hidden

Ask about a topic that would require a *full-length* City Council meeting (not a short committee meeting) that hasn't been transcribed — e.g. anything discussed only in one of the 3+ hour Council sessions.

Expect: `not_in_corpus: true`. Talking point: this isn't a bug — full Council sessions exceed `whisper-1`'s 25MB limit at this Granicus instance's real bitrate (DECISIONS #116), a real, dated, understood gap with a named (not-yet-authorized) fix, not an unknown unknown.

## 6. Health check

`GET /health` — expect `{"status": "ok", "db": "ok"}`, HTTP 200. Kill the DB connection (or point `LOCAL_DB_HOST` at a bad host) to show the `503`/`degraded` path if time allows.
