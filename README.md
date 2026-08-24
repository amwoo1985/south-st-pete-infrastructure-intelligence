# Blaq Blob

*(Formerly "South St. Petersburg Infrastructure Intelligence," formerly "cba-rag-assistant" — see `DECISIONS.md` #9, #136.)*

A research tool that reads through local government meeting records, grant listings, and transcribed City Council audio so you can ask it a plain question and get back an answer with a real citation attached — not a guess, not a summary from memory, an answer traced back to the actual document or meeting it came from. Built for the South St. Petersburg Energy Coalition's Community Benefits Agreement negotiation with the City of St. Petersburg and Duke Energy Florida, and built to keep working for whoever runs this negotiation next, not just for the person who built it.

Every non-trivial decision made while building this — including this rename — is recorded, numbered, and dated in [`DECISIONS.md`](DECISIONS.md). This README is the summary. If you're taking this over and want to know *why* something works the way it does, not just *that* it does, `DECISIONS.md` is the place to look — search it for the decision number cited next to any claim below.

## What it does

Ask a plain-language question about local grants, meeting agendas, or City Council business. The system:

1. Looks through the archive for the pieces of text most relevant to your question.
2. Writes an answer using only what it actually found — never fills gaps from general internet knowledge.
3. Attaches a citation to every claim: source URL, publish date, and a human-readable section label.
4. If nothing relevant is in the archive, it says so plainly instead of guessing.

You can also upload a document (PDF/DOCX/TXT — a CBA draft, a memo, anything) and ask questions about it within seconds, through the exact same citation-and-grounding process as everything else in the archive.

## If you're taking this over, start here

A few questions a successor would reasonably ask, answered plainly:

**Why won't it just answer from what it "knows"?** Because in a negotiation, a confident-sounding wrong answer is worse than no answer — it can get repeated in a meeting and used against you. The system is built so it can *only* speak from documents it can point back to. If it can't find the answer in the archive, it tells you that instead of making something up. This rule has no exceptions, anywhere in the system.

**Why does it only pull from a fixed list of sources instead of searching everything?** An open-ended web crawler is a liability — it can get this project blocked from a site, or quietly pull in something unreliable without anyone noticing. The source list is closed on purpose (see `DECISIONS.md` #11); adding a new one is a deliberate decision, not something that happens by accident.

**Why did the City Council audio take so long to work right?** The hosted transcription service has a hard 25MB-per-file limit, and a full 3+ hour Council meeting recording is 150-230MB. Early on, full meetings just failed outright (a known, disclosed gap — `DECISIONS.md` #86, #116). That's fixed now: the system automatically splits long audio into safe-sized pieces at real audio boundaries (never a raw byte cut, which could corrupt a word mid-split) before sending each piece off, then stitches the transcript back together. Verified against a real 3-hour-plus meeting — see `DECISIONS.md` #130.

**What does this cost to run, and who's watching that?** The database and app run in the cloud 24/7, and transcribing months of Council audio isn't free. There's a live monthly budget alert (see "Ongoing operations" below) that emails a warning at 50%, 90%, and 100% of a $100/month threshold — check that inbox. If the CBA work picks up and costs climb, the threshold in `.env` is a one-line change plus a re-run of `infra/register_cost_alerts.sh`.

**What breaks on its own, and how would I know?** A crawler will break the day its target website changes its layout — that's normal, expected, and it's designed to fail loudly (an error in the job's status, never a silent "nothing new found"). There's no dashboard for this yet; checking means looking at the crawler/transcription job tables directly. Named as a real gap below, not hidden.

**Who else can reach this thing?** The live deployment has a public web address and the document-upload endpoint has no login requirement — a deliberate, disclosed shortcut, not an oversight. Don't upload a real, sensitive negotiation draft to the live instance until that's addressed.

## Architecture

```
Sources (closed list, DECISIONS #11)
  ├─ Tier 1 (static HTML crawlers): Legistar, stpete.org grants, Pinellas CF grants,
  │  Pinellas HCD, SPHA, St. Pete Council votes
  └─ Tier 1.5 (Granicus sub-pipeline): RSS-adjacent meeting discovery → human-resolved
     MP3 URL → async transcription worker (whisper-1, duration-aware splitting for
     long meetings, DECISIONS #130) → transcript
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

One Docker image (`Dockerfile`), role selected by `docker-compose.yml`'s `command:` per service (`api` / `worker`) — not separate builds (DECISIONS #8, #107). In production this runs on AWS Fargate (`api` and `worker` as separate always-on services) with Postgres on RDS — see "Running it live" below.

A weekly automated recrawl (every Saturday, DECISIONS #126/#134) keeps the archive current without anyone needing to remember to run it by hand.

## Why these sources, not others

- **A named, finite source list, not "crawl municipal/county/state."** An open-ended civic-site crawler is an unbounded liability — rate-limit risk, silent scope creep, unverifiable provenance. Adding a source requires a new `DECISIONS.md` entry before any code (DECISIONS #11).
- **Florida PSC** — deferred. A direct fetch returned near-empty content, likely JS-rendered or bot-blocked; needs a headless-browser investigation not done this cycle.
- **GovTrack** — deferred. A direct fetch returned a confirmed HTTP 403. GovTrack is believed to offer a public data API — the right integration path, but unverified.
- **Florida Statutes** — deliberately *not* a crawler target. Statutes change on a legislative-session cadence, not a meeting cadence, and the source is ColdFusion/session-URL-dependent — not a stable scheduled-crawl shape. Modeled instead as a separate, smaller, not-yet-built "fetch and cache by citation" capability, fitted to how the source actually changes rather than forced into the crawler pattern.
- **Granicus (`stpete.granicus.com`)** — the one source that hit a real access wall mid-build: its robots.txt disallows this project's honest, non-spoofed User-Agent from the entire host (DECISIONS #77). Rather than drop the source or spoof a browser UA to route around a stated policy (`.claude/rules/crawler.md` treats a block as a stop signal, not an obstacle), meeting discovery was redesigned around a human (or, with explicit sign-off, an AI-driven real browser session — DECISIONS #115) resolving each meeting's real MP3 URL directly; the actually-automatable, actually-expensive half of the pipeline — download and transcribe — was never blocked and stays fully automated (DECISIONS #79-82).

## Provenance and attribution model

Every chunk in the archive — crawled, transcribed, or uploaded — carries, at minimum:

- **Source URL** — the exact page or MediaPlayer.php URL the content came from (or `upload://<sha256>` for a live upload).
- **Published/effective date** — the meeting date or the grant's posted date, not just when this system happened to fetch it.
- **Retrieval timestamp** — when this system actually captured the content.

Every citation returned by `/query` resolves to a real row via `GET /sources/{doc_id}`, traceable back through the same fields. This discipline exists because a civic-data tool with wrong or unattributed provenance is actively worse than no tool (`.claude/rules/crawler.md`).

## Full-length City Council meetings: fixed, not a known gap anymore

Earlier in this build, every full-length Council meeting (3+ hours, 150-230MB) failed to transcribe outright — the hosted transcription API has a hard 25MB single-request limit, and a real validation run (DECISIONS #116) confirmed this wasn't an edge case, it was nearly every real session. The system now automatically splits long audio into safe-sized pieces at real decoded-audio boundaries (never a raw byte cut of the compressed file, which risks corrupting a word mid-split) before transcribing each piece, then stitches the results back together — with a hard per-piece size check and automatic re-splitting if a piece still comes out too big.

This was proven against a real full-length meeting, not a short test clip: a previously-failed 3.24-hour session was reprocessed end to end — split into 7 pieces, transcribed, stitched, chunked, embedded — for a real cost of about $1.17, and a real question about that meeting's actual content returned the correct, cited answer. Full details and the exact numbers: `DECISIONS.md` #130.

The 12-month Granicus backfill is unblocked by this fix.

## Ongoing operations — what's automatic, what still needs a human

- **Recrawl:** automatic, weekly, every Saturday 06:00 UTC (DECISIONS #126, #134) — pulls fresh Tier-1/1.5 content without anyone triggering it by hand.
- **Cost alert:** automatic and live — a $100/month AWS budget alert emails warnings at 50%, 90%, and 100% of actual spend (DECISIONS #133). Change the threshold in `.env` and re-run `infra/register_cost_alerts.sh` if it needs adjusting.
- **Crawler health:** not automatic. A crawler breaks the moment its target site's layout changes, and it fails loudly (an explicit error, never a silently-empty result — `.claude/rules/crawler.md`) rather than pretending nothing happened, but there's no dashboard yet — checking means looking at the job-status tables directly. A named future improvement, not built.
- **pgvector maintenance:** the vector search index benefits from periodic maintenance as the archive grows, especially once a full year of transcripts has landed.
- **Upload access:** `POST /documents/upload` has no login requirement — a known, deliberate shortcut, not an oversight. Don't upload an actually-sensitive negotiation document to the live, publicly-reachable instance until that's addressed.
- **Secrets hygiene:** if you're the one running commands against `.env` or any file with a live API key/password in it, read `.claude/rules/secrets.md` first — two real key exposures happened during this build from printing a secrets file without shaping the output, and the fix is a habit, not a one-time patch.

## Running it locally

```
docker-compose up --build
```

Brings up Postgres+pgvector, the API (port 8000), and the transcription worker with one command. Static UI served at `http://localhost:8000/`.

## Running it live

The production deployment runs on AWS Fargate — an `api` service and a `worker` service, each its own always-on container, talking to a managed Postgres (RDS) database. `infra/DEPLOY_NEXT_STEPS.md` is the operational runbook: how to re-deploy after a code change, how to load newly-crawled content into the live database, and the exact commands for both. The live app's public address changes if a task restarts (it isn't a fixed URL yet — a real future improvement, not a blocker); the runbook explains how to look it up.

## Stack

Python 3.12, FastAPI, PostgreSQL + pgvector, Docker/docker-compose (local) and AWS Fargate/ECS + RDS (deploy target, DECISIONS #1). OpenAI for embeddings (`text-embedding-3-small`), generation (`gpt-4o-mini`), and transcription (`whisper-1`) — one provider, one account, one bill (DECISIONS #13).
