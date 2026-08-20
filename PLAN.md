# Implementation Plan: South St. Petersburg Infrastructure Intelligence

*Supersedes the original `PLAN.md` (which was scoped as a smaller, demo-only, job-application-deadline-bound project). This project is now fully decoupled from the CV/AI-analysis deadline — see `CLAUDE.md` and DECISIONS #9. Day 1 = 2026-08-19 (today), Day 9 = 2026-08-27.*

**Honest top line:** the full committed scope — 3 Tier-1 scrapers + the Granicus transcription pipeline + RAG + live upload + a web UI + Docker + Fargate deploy, at real-production quality — does not fit cleanly in 9 partial days, even agentically directed. Rough phase-effort sums to ~11-12 build-days if every phase runs to full completion serially. The plan below fits 9 days **only** because the "ongoing tool, not a one-shot demo" framing genuinely licenses one thing: the Granicus 12-month backfill does not need to *finish* inside the 9-day window, only to be *built, validated on a small batch, and started* — it keeps running afterward since Amber will keep using this. Section 3 names the fallback order if even that isn't enough.

---

## New DECISIONS entries to write Day 1 (not designed here — flagged so they aren't skipped)

- **#9-#12 already written** (project reframe, UI/upload scope, source list, transcription pattern).
- Still open, lock Day 1: embedding/LLM provider choice (after smoke test), hosted transcription API provider choice (after smoke test).

---

## 1. Phase Breakdown and "Done" Criteria

| # | Phase | Done = |
|---|---|---|
| A | Repo/infra scaffold + provider decisions | AWS creds verified + a trivial container actually ran on Fargate and reached RDS; embedding/LLM API key smoke-tested (one real embed call + one real completion call); transcription API key smoke-tested (one real short audio clip transcribed); provider DECISIONS entries written; `crawler.md` + `crawler`/`crawler-review` agents present; `git init` done. |
| B | Tier-1 crawlers (Legistar, stpete.org grants, Pinellas CF grants) | All three run against the live sites, respect robots.txt/rate limits, and every stored item carries source URL + published/effective date + retrieval timestamp. Legistar prefers Accessible-Agenda HTML over PDF when both exist; PDF fallback works when HTML isn't offered. Structure changes fail loud (raise/log), never return silently empty. |
| C | Granicus Tier-1.5 sub-pipeline | RSS feed parsed into a job queue (title + pubDate per meeting); MP3 link extracted per meeting page; transcription jobs table with status column, `FOR UPDATE SKIP LOCKED` claiming, stale-row recovery — explicitly modeled on the R-EX async worker pattern; hard 12-month backfill bound enforced in code; a small initial batch (2-4 weeks) transcribed end-to-end and verified before the full backfill runs unattended. |
| D | Ingestion/chunking (crawled text + uploaded docs + transcripts) | One chunking module handles all three content shapes (HTML/PDF prose, agenda-item structure, transcript-with-timestamps) with domain-aware boundaries, attaches source/doc-type/date metadata, idempotent re-run. |
| E | Embeddings + pgvector | Embeddings generated and stored with an ivfflat/HNSW index; similarity search returns sensibly ranked chunks against the real Tier-1 + a sample Tier-1.5 corpus, not synthetic test data. |
| F | Retrieval/RAG logic | Relevance-threshold filter (not blind top-k), near-duplicate dedup, explicit token budget cap, source citations attached to every retrieved chunk. |
| G | LLM generation | Grounding-contract prompt live: answers only from retrieved context, cites sources, explicitly says "not in corpus" when retrieval is empty/below threshold — verified with at least one real query that *should* return "not in corpus." Timeout/failure degrades gracefully. |
| H | FastAPI layer incl. live upload | `POST /query`, `GET /health`, `GET /sources/{doc_id}`, `POST /documents/upload` (PDF/DOCX/TXT). Upload is idempotent on file hash, parses/chunks/embeds as a background task (not blocking the request); an uploaded doc is queryable with citations immediately after. |
| I | Minimal web UI | A page to ask a question and see a cited answer; functional upload path (Swagger/API is an acceptable fallback if UI time runs short). No design system, just usable. |
| J | Docker | `Dockerfile`(s) + `docker-compose.yml` bring up API + transcription worker + Postgres/pgvector + UI with one command; image role selected by env var (no separate builds, per DECISIONS #8 / `deploy.md`). |
| K | AWS Fargate deploy | App reachable at a public URL; worker service running as its own Fargate service/task; RDS confirmed pgvector-enabled and reachable only from the app's network; secrets via env/secrets manager, none baked into the image. |
| L | Docs/demo | README covering architecture, provenance/attribution model, why these sources over PSC/GovTrack/FL Statutes, ongoing-maintenance notes, cost notes, and a demo script/recording as insurance against live-deploy flakiness. |

---

## 2. Day-by-Day Schedule (Day 1 = 2026-08-19 → Day 9 = 2026-08-27)

| Day | Date | Focus |
|---|---|---|
| **1** | **08-19 (today)** | **Smoke-test everything unverified.** AWS: creds + a trivial container actually run on Fargate, reach RDS. Embedding/LLM provider: pick candidate, real embed + completion call. Transcription API: pick candidate, transcribe a real short Granicus MP3 clip end-to-end. Repo scaffold, `crawler.md` + `crawler`/`crawler-review` agents in place, provider DECISIONS entries written. Recon (not code) on Legistar's actual table markup. |
| **2** | 08-20 | Shared crawler base (robots.txt, rate limit, real User-Agent, attribution metadata, fail-loud). Build Legistar crawler end to end (HTML table → dates, agenda HTML preferred over PDF, minutes/video links, PDF-text fallback). |
| **3** | 08-21 | stpete.org grants/loans crawler (6 categories) + Pinellas Community Foundation grants crawler (5 programs + dated 2026 table). `crawler-review` pass on all three Tier-1 sources. Start unified chunking module. |
| **4** | 08-22 | Finish chunking; embeddings + pgvector schema/index/insert pipeline; validate similarity search against the real Tier-1 corpus with hand-picked queries. Start Granicus: RSS parser + per-meeting MP3 link extractor + jobs-table schema (status column, `SKIP LOCKED`, stale-row recovery). |
| **5** | 08-23 | **CHECKPOINT DAY.** Finish Granicus job worker (claim → call transcription API → store transcript). Run initial small backfill batch (2-4 weeks) to validate the full pipeline; explicitly do **not** kick off the full 12-month backfill unattended yet. **Go/no-go:** if Tier-1 isn't solid or retrieval/grounding hasn't started, invoke the Section 3 fallback order now, not later. |
| **6** | 08-24 | Retrieval logic (threshold, dedup, token cap, citations) + grounding-contract prompt + failure handling. Verify with real queries, including one that should legitimately return "not in corpus." |
| **7** | 08-25 | FastAPI: `/query`, `/health`, `/sources/{doc_id}`, `/documents/upload` (idempotent, background-embedded). Verify: upload a real PDF, query it immediately, get a cited answer. Start minimal web UI. |
| **8** | 08-26 | Finish web UI (ask box + citations, upload form if time allows). Docker: full local stack up with one command. Start Fargate deploy (task defs for API + worker services, RDS wiring, secrets). *(Note: this is also the CV/analysis deadline day — that work is on a completely separate track and does not draw from this day's time.)* |
| **9** | 08-27 | Finish Fargate deploy — public URL reachable, worker service running, end-to-end query verified against the deployed stack. Kick off the full 12-month Granicus backfill as an ongoing background job (does not need to finish today). README, `crawler.md` finalized, demo recording, final bug-fix buffer. |

**Note on "done" for Day 9:** the tool is live and answering grounded, cited queries over the full Tier-1 corpus and at least a validated slice of Granicus transcripts, with the full backfill continuing to run afterward — the honest shape of "done" for an ongoing operational tool, not a demo that needs every meeting transcribed by a deadline.

---

## 3. Load-Bearing vs. Stretch, and the Fallback Cut Order

| Phase | Load-bearing | Stretch / cut first |
|---|---|---|
| Tier-1 crawlers | Legistar + stpete.org grants working, attributed | Pinellas CF grants (cut third source before cutting UI or upload) |
| Granicus Tier-1.5 | Pipeline built, validated on small batch, backfill running | Full 12-month backfill *completing* by Day 9; agenda-item-to-timestamp mapping (already a nice-to-have) |
| RAG core (E/F/G) | Threshold retrieval, dedup, grounding contract, citations | Query rewriting, multi-hop retrieval, reranking |
| API + upload | `/query`, `/health`, `/documents/upload` working end to end | Auth, rate limiting, pagination |
| Web UI | A functional ask-and-see-citations page | Upload form in the UI (fall back to Swagger/API for uploads), any visual styling |
| Docker/Fargate | Local compose + live public URL | Custom domain, ACM/HTTPS, autoscaling, CI/CD |
| Docs | README with architecture + attribution + why-these-sources | Diagrams beyond ASCII, blog-style writeup |

**Fallback cut order if the Day 5 checkpoint shows slippage, in order:**

1. Narrow the Granicus backfill batch further (down to 1-2 meetings) and let the full 12 months run as a documented background job past Day 9.
2. Drop the agenda-item-to-timestamp investigation entirely (already a nice-to-have, never assumed).
3. Cut UI polish: no upload form in the UI, no styling — ask box + citations only; uploads go through Swagger/curl.
4. Cut Pinellas Community Foundation grants (the third Tier-1 source) — document it as a defined next-source addition requiring its own DECISIONS entry.
5. Cut Fargate niceties — public task IP or a bare ALB, no custom domain/HTTPS.
6. Absolute last resort: cut live cloud deploy to "runs in Docker locally + full deploy writeup naming the exact blocker" — Day 1's Fargate smoke test exists specifically so this doesn't become a late surprise.
7. **Never cut:** the grounding contract (DECISIONS #6/#7), source attribution (mandatory per `crawler.md`), or the live upload endpoint itself — these distinguish this from a static demo.

---

## 4. Ongoing-Operation Notes (this is not a one-off demo)

- **Maintenance ownership:** every crawler breaks when its target site's structure changes. `crawler.md`'s fail-loud rule only helps if someone notices — ship at minimum a `/health/crawlers` endpoint or a job-status table, not a full alerting system, named as a Day-9-or-later follow-up if there's no time now.
- **Recrawl cadence:** not decided in this plan — name it as an open item needing its own DECISIONS entry once crawlers are stable; don't let a cron cadence get silently assumed.
- **Recurring cost, named not solved:** Fargate services (API + worker) don't scale to zero the way Lambda does — 24/7 uptime has a real ongoing cost, same for RDS. Transcription API usage across 12 months of multi-hour council audio is likely the single largest cost line — set a budget alert on both AWS billing and the transcription provider's usage dashboard before the unattended full backfill runs, not after a surprise bill.
- **pgvector maintenance:** index needs periodic reindex/maintenance as the corpus grows, especially once a year of transcripts lands.
- **Upload governance:** the live upload endpoint has no auth (explicit stretch cut). Before any actually-sensitive negotiation document goes through the deployed instance, that needs a conscious "who else can reach this URL" decision.

---

## 5. Interview-Defense Talking Points (additive to the original nine)

10. **"Why a named, finite source list instead of an open-ended crawler?"** — An unbounded civic-site crawler is an unbounded liability: rate-limit/ToS risk, silent scope creep, unverifiable provenance. Naming exact sites, verifying each is actually scrapeable, and requiring a new DECISIONS entry to add one more mirrors the same discipline as the grounding contract.
11. **"Why Tier 1 before PSC or GovTrack?"** — Verified feasibility first: PSC returned near-empty content (suspected JS-rendering/blocking), GovTrack returned a confirmed HTTP 403. Building against sources already proven to work before sinking time into a headless-browser or API-terms investigation is a sequencing decision grounded in actual evidence, not guesswork.
12. **"Why isn't Florida Statutes part of the crawler?"** — Cadence mismatch: statutes change on a legislative-session cadence, not a meeting cadence, and the source (ColdFusion, session-URL-dependent) isn't a stable scheduled-crawl target. Modeled instead as on-demand "fetch and cache this citation when a RAG answer needs it" — fitting the ingestion pattern to the source's real shape.
13. **"Walk me through the transcription pipeline"** — Granicus MP3 → hosted STT → transcript, architected as an async worker exactly like the production pattern built at VideoAmp/R-EX: status column, `FOR UPDATE SKIP LOCKED` claiming, stale-row recovery. Not a forced analogy — multi-hour audio genuinely can't transcribe synchronously in an HTTP request.
14. **"How do you keep transcription costs bounded?"** — Hard 12-month backfill bound in code, a deliberate small-batch validation run before letting the full backfill go unattended, a named budget-alert follow-up. Cost-awareness as a first-class constraint, not a surprise-bill discovery.
15. **"How does live upload fit the grounding contract?"** — An uploaded document goes through the identical chunk/embed/pgvector/threshold-retrieval/citation path as crawled content. The contract doesn't relax for user-supplied content — a live negotiation draft is exactly where a hallucinated or misattributed answer would be actively harmful, not just embarrassing.

---

### Anchor Files

- `DECISIONS.md` — #9-#12 written; provider choices land here Day 1
- `.claude/rules/crawler.md` — governs all scraping/attribution/backfill-bound behavior; must exist before Phase B starts
- `app/rag/retrieval.py` — threshold/dedup/citation logic, now serving four content shapes (crawled HTML/PDF, transcripts, uploads)
- `app/api/main.py` — carries the live upload endpoint
- `.claude/rules/deploy.md` — Fargate/secrets/idempotency rules, now also governing the worker service
