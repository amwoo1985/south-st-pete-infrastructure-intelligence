# South St. Petersburg Infrastructure Intelligence

A RAG tool over local government meeting records, grants, and transcribed City Council audio — built for the South St. Petersburg Energy Coalition's Community Benefits Agreement negotiation with the City of St. Petersburg, residents, and Duke Energy Florida. This is a real operational tool used to ground CBA negotiation research in cited primary sources, not a portfolio demo.

Every non-trivial design decision made while building this is recorded, numbered, and dated in [`DECISIONS.md`](DECISIONS.md) — currently 118 entries. This README summarizes; `DECISIONS.md` is the record of record.

## What it does

Ask a plain-language question about local grants, meeting agendas, or City Council business. The system:

1. Embeds the question and retrieves the most relevant chunks from a corpus of crawled/transcribed content, filtered by a similarity threshold (never blind top-k).
2. Generates an answer using only the retrieved context — never the model's general knowledge.
3. Returns the answer with a citation for every claim: source URL, publish date, and a human-readable section label.
4. If nothing relevant is in the corpus, says so explicitly (`not_in_corpus: true`) instead of guessing.

A live document can also be uploaded (PDF/DOCX/TXT) and is queryable with citations within seconds, through the identical retrieval/grounding path as crawled content — the grounding contract doesn't relax for user-supplied text.

## Architecture

```
Sources (closed list, DECISIONS #11)
  ├─ Tier 1 (static HTML crawlers): Legistar, stpete.org grants, Pinellas CF grants,
  │  Pinellas HCD, SPHA, St. Pete Council votes
  └─ Tier 1.5 (Granicus sub-pipeline): RSS-adjacent meeting discovery → human-resolved
     MP3 URL → async transcription worker (whisper-1) → transcript
       ↓
Chunking (app/chunking/) — one Chunk shape, per-source-shape boundary logic
  (agenda-item, h2-section, table-row, sentence-grouped transcript, uploaded-doc paragraph)
       ↓
Embeddings (app/embeddings/) — text-embedding-3-small, pgvector storage, idempotent
  chunk_id pre-check before every paid API call
       ↓
Retrieval + grounding (app/rag/) — similarity threshold, near-dup dedup, token cap,
  static system prompt, citations traced to real chunk rows
       ↓
FastAPI (app/api/) — /query, /health, /sources/{doc_id}, /documents/upload
       ↓
Static web UI (app/api/static/) — ask box, citations, upload form, served by the same app
```

One Docker image (`Dockerfile`), role selected by `docker-compose.yml`'s `command:` per service (`api` / `worker`) — not separate builds (DECISIONS #8, #107).

## Why these sources, not others

- **A named, finite source list, not "crawl municipal/county/state."** An open-ended civic-site crawler is an unbounded liability — rate-limit risk, silent scope creep, unverifiable provenance. Adding a source requires a new `DECISIONS.md` entry before any code (DECISIONS #11).
- **Florida PSC** — deferred. A direct fetch returned near-empty content, likely JS-rendered or bot-blocked; needs a headless-browser investigation not done this cycle.
- **GovTrack** — deferred. A direct fetch returned a confirmed HTTP 403. GovTrack is believed to offer a public data API — the right integration path, but unverified.
- **Florida Statutes** — deliberately *not* a crawler target. Statutes change on a legislative-session cadence, not a meeting cadence, and the source is ColdFusion/session-URL-dependent — not a stable scheduled-crawl shape. Modeled instead as a separate, smaller, not-yet-built "fetch and cache by citation" capability, fitted to how the source actually changes rather than forced into the crawler pattern.
- **Granicus (`stpete.granicus.com`)** — the one source that hit a real access wall mid-build: its robots.txt disallows this project's honest, non-spoofed User-Agent from the entire host (DECISIONS #77). Rather than drop the source or spoof a browser UA to route around a stated policy (`.claude/rules/crawler.md` treats a block as a stop signal, not an obstacle), meeting discovery was redesigned around a human (or, with explicit sign-off, an AI-driven real browser session — DECISIONS #115) resolving each meeting's real MP3 URL directly; the actually-automatable, actually-expensive half of the pipeline — download and transcribe — was never blocked and stays fully automated (DECISIONS #79-82).

## Provenance and attribution model

Every chunk in the corpus — crawled, transcribed, or uploaded — carries, at minimum:

- **Source URL** — the exact page or MediaPlayer.php URL the content came from (or `upload://<sha256>` for a live upload).
- **Published/effective date** — the meeting date or the grant's posted date, not just when this system happened to fetch it.
- **Retrieval timestamp** — when this system actually captured the content.

Every citation returned by `/query` resolves to a real row via `GET /sources/{doc_id}`, traceable back through the same fields. This discipline exists because a civic-data tool with wrong or unattributed provenance is actively worse than no tool (`.claude/rules/crawler.md`).

## Real, load-bearing limitation: most City Council meetings can't transcribe yet

A live validation run (DECISIONS #116) registered 3 real, recent City Council sessions and attempted transcription for real. All 3 failed identically and correctly: each file is 150–230MB, and `whisper-1` has a hard 25MB single-request limit. At this Granicus instance's real encoding bitrate (~138 kbps, measured from real files, not assumed), that ceiling works out to roughly 25 minutes of audio — a limit nearly every full Council session exceeds. The worker fails loud (a clear stored `failure_reason`, no wasted spend — it aborts on an 8MB size probe before ever calling the paid API) rather than attempting a byte-offset split that could silently corrupt an MP3 frame boundary and produce a plausible-looking but wrong transcript.

Two shorter real committee meetings (14 minutes each) validated the full successful path instead — transcribed, chunked, embedded, and confirmed queryable with a correct, cited answer (DECISIONS #116, #117).

**Fixing this for real Council-length audio requires duration-aware chunked transcription** (splitting audio at real frame/silence boundaries — needs an `ffmpeg`/`pydub`-class dependency, a new Docker image dependency, and its own sign-off) — flagged as an open item since DECISIONS #86, confirmed as the actual blocking case (not a rare edge case) by this real validation run. The full 12-month backfill is **not** unblocked until this is resolved.

## Ongoing-operation notes (this is not a one-off demo)

- **Crawler maintenance.** Every crawler breaks when its target site's structure changes. `.claude/rules/crawler.md`'s fail-loud rule (raise/log, never a silently-empty result) is the current safety net; a dedicated crawler health view is a named future improvement, not yet built.
- **Recrawl cadence.** Not yet decided — an open item pending its own `DECISIONS.md` entry once the crawler set has been running long enough to have a real answer.
- **Recurring cost.** Fargate (API + worker services) and RDS run 24/7 — real, if modest, ongoing cost that doesn't scale to zero the way a serverless target would (a deliberate tradeoff, DECISIONS #1). Whisper transcription across a full 12 months of multi-hour Council audio is likely the single largest real cost line once the size-limit blocker above is resolved — a budget alert should exist on both AWS billing and OpenAI's usage dashboard before that backfill ever runs unattended.
- **pgvector maintenance.** The HNSW index benefits from periodic maintenance as the corpus grows, especially once a year of transcripts lands.
- **Upload governance.** `POST /documents/upload` has no authentication yet — a known, deliberate stretch-scope cut, not an oversight. Do not upload actually-sensitive negotiation documents to a publicly reachable deployed instance until that's addressed.

## Interview-defense talking points

- **Why a named, finite source list instead of an open-ended crawler?** An unbounded civic-site crawler is an unbounded liability. Naming exact sites and requiring a new `DECISIONS.md` entry to add one more mirrors the same discipline as the grounding contract itself.
- **Why Tier 1 before PSC or GovTrack?** Verified feasibility first — PSC returned near-empty content, GovTrack returned a confirmed 403. Building against sources already proven to work, before sinking time into a headless-browser or API-terms investigation, is sequencing grounded in evidence, not guesswork.
- **Walk me through the transcription pipeline.** Granicus MP3 → hosted speech-to-text → transcript, as an async worker: status column, `FOR UPDATE SKIP LOCKED` claiming, stale-row recovery — the same pattern proven in production for exactly the reason multi-hour audio can't transcribe synchronously inside an HTTP request.
- **How do you keep transcription costs bounded?** A hard 12-month backfill bound enforced in code, a deliberate small-batch validation run before any unattended backfill, and — as that validation run just proved — a real, named blocker (the 25MB size limit) caught with real files and $0 spent, instead of discovered mid-backfill.
- **How does live upload fit the grounding contract?** Identically to crawled content — same chunk/embed/threshold-retrieval/citation path. The contract doesn't relax for user-supplied text; a live negotiation draft is exactly where a hallucinated or misattributed answer would be actively harmful.
- **What's the single biggest known gap right now?** Full-length City Council meeting transcription — see the limitation section above. It's understood, dated, and has a named (not yet authorized) fix, rather than being an unknown unknown.

## Running locally

```
docker-compose up --build
```

Brings up Postgres+pgvector, the API (port 8000), and the transcription worker with one command (DECISIONS #107-#109). Static UI served at `http://localhost:8000/`.

## Stack

Python 3.12, FastAPI, PostgreSQL + pgvector, Docker/docker-compose (local) and AWS Fargate/ECS + RDS (deploy target, DECISIONS #1). OpenAI for embeddings (`text-embedding-3-small`), generation (`gpt-4o-mini`), and transcription (`whisper-1`) — one provider, one account, one bill (DECISIONS #13).
