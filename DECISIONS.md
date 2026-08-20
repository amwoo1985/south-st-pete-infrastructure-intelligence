# DECISIONS

Numbered log of every binding design decision for South St. Petersburg Infrastructure Intelligence. New entries append; nothing here gets silently rewritten — if a decision changes, add a new entry that supersedes the old one and say so.

## #1 — AWS deploy target: Fargate, not Lambda
Decision: Deploy via ECS Fargate (container + task definition + service) with RDS/managed Postgres, not Lambda+API Gateway.
Why: Workload holds a persistent DB connection pool and isn't bursty/event-driven — poor fit for Lambda's execution model. Fargate also supports "same container locally and in prod," a cleaner and more defensible cloud-native story than a Lambda adapter shim (Mangum).
Date: 2026-08-19

## #2 — No Kubernetes, no Terraform/IaC this build cycle
Decision: Scope out Kubernetes and Terraform entirely for this project.
Why: Kubernetes is on the Principal posting's requirement list, not the Manager role being targeted. Standing up a real cluster and getting it demo-stable costs 2-3 of the ~6 available build days. A shallow K8s/IaC setup that can't survive live interview questioning is worse than not having it — the gap is more defensible than a thin claim.
Date: 2026-08-19

## #3 — Vector store: pgvector, not a dedicated vector DB
Decision: Use Postgres + pgvector for embeddings/retrieval rather than Pinecone, Weaviate, or similar.
Why: Already have operational Postgres experience from VideoAmp/R-EX. Avoids a second infra dependency at this project's scale. pgvector is genuinely production-viable here; dedicated vector DBs earn their keep at a scale (tens of millions of vectors, managed sharding) this project doesn't reach.
Date: 2026-08-19

## #4 — Domain corpus: own CBA/grant/council documents
Decision: The RAG corpus is built from Amber's actual Community Benefits Agreement precedents, grant guidelines, and city council documents (real, or representative synthetic reconstructions where the source is confidential).
Why: A domain-tied project is authentic and defensible in an interview; a generic tutorial corpus is not. Where real documents are confidential, synthetic-but-representative equivalents are honestly labeled as such — same clean-room principle used for the R-EX reference notes.
Date: 2026-08-19

## #5 — Agentic build workflow: AI-directed implementation, human-owned architecture
Decision: AI coding agents (via the `cba-rag` orchestrator and its specialists) write the implementation; Amber owns architecture, retrieval-quality decisions, prompt design, cloud topology, and every cut-scope call.
Why: Same working pattern as the VideoAmp/R-EX role this project is meant to evidence. Ownership of judgment calls is the actual signal for an "Agentic Developer" narrative, not line-by-line hand-typing.
Date: 2026-08-19

## #6 — Grounding contract: answer from context only, cite sources, no silent hallucination
Decision: Every generation path must answer only from retrieved context, cite the source chunk(s), and explicitly state when the answer isn't in the corpus rather than guessing.
Why: This is the core "LLM fundamentals" proof point for the application. An ungrounded fallback would undercut the entire premise of the demo.
Date: 2026-08-19

## #7 — Retrieval uses a relevance threshold, not blind top-k
Decision: Retrieval filters candidate chunks by a similarity threshold and deduplicates near-duplicates before context assembly, on top of top-k ranking.
Why: Avoids context-stuffing — irrelevant chunks degrade answer quality and cost, and "why not just top-k" is a named interview-defense question in PLAN.md section 4.
Date: 2026-08-19

## #8 — No secrets in the Docker image; config-driven environments
Decision: Secrets are injected via environment variables or a secrets manager at runtime, never committed or baked into the image. Environment differences (dev/prod) are config-driven off one image, not separate builds.
Why: Standard hygiene; mirrors the config pattern already used at VideoAmp/R-EX and documented in the clean-room reference notes.
Date: 2026-08-19

## #9 — Project is a real production tool, not a demo; decoupled from the job-application deadline
Decision: Renamed to "South St. Petersburg Infrastructure Intelligence." This is a tool Amber will actually use for ongoing civic/coalition work, not a throwaway portfolio artifact. It gets its own 9-day build window (Day 1 = 2026-08-19, Day 9 = 2026-08-27), fully independent of the CodeBoxx job application's CV + AI-analysis deadline (2026-08-26 12PM) — those two deliverables are a separate track, handled entirely outside this project's schedule.
Why: Conflating a hard, external deadline with an open-scope real-tool build is exactly the ambiguity that cost ~2 months on R-EX. Separating the tracks means neither the CV/analysis nor this project's quality gets rushed by the other's clock.
Date: 2026-08-19

## #10 — Web UI and live document upload are in scope
Decision: A minimal web interface (ask a question, see a cited answer) and a live `POST /documents/upload` endpoint (PDF/DOCX/TXT) are in scope this cycle — superseding the original API-only, static-corpus framing.
Why: Amber will use this operationally, including uploading her own working documents (e.g. a CBA draft in progress). A static demo corpus alone doesn't serve that.
Date: 2026-08-19

## #11 — Crawler source list: named and finite, not "municipal/county/state"
Decision: This cycle's crawler targets exactly these sources, verified working this session:
- **Tier 1** (static HTML, confirmed clean): Pinellas County BCC meetings via Legistar (`pinellas.legistar.com/Calendar.aspx`); City of St. Petersburg grants/loans (`stpete.org/residents/grants___loans/index.php`); Pinellas Community Foundation grants (`pinellascf.org/nonprofits/grants/`).
- **Tier 1.5** (confirmed working, separate sub-pipeline): City of St. Petersburg City Council meetings via Granicus (`stpete.granicus.com`) — see #12.

Explicitly deferred, NOT built this cycle (each needs its own future DECISIONS entry before work starts):
- Florida PSC schedule of events — a fetch returned near-empty content this session; suspected JS-rendering or bot-blocking. Needs a headless-browser investigation first.
- GovTrack federal bills — a direct fetch returned HTTP 403 (confirmed blocked) this session. GovTrack is believed to offer a public data API/bulk export (unverified this session) — that's the right integration path, not HTML scraping, but needs real verification before building.
- Florida Statutes — explicitly NOT a crawler target. Changes on a legislative-session cadence, not a meeting cadence; ColdFusion session-URL-dependent. Modeled instead as a separate, smaller on-demand "fetch/cache by citation" capability.

Any addition to this list requires a new DECISIONS.md entry — no silent scope expansion.
Why: An unbounded "crawl municipal/county/state" mandate is an unbounded liability. A named, session-verified list keeps the crawler's scope honest and testable.
Date: 2026-08-19

## #12 — Granicus transcription: async worker pattern, hard 12-month backfill bound, hosted API
Decision: St. Petersburg City Council meeting audio (direct MP3 links resolved per meeting via Granicus's RSS feed + MediaPlayer pages) is transcribed via a **hosted** speech-to-text API (specific provider TBD, to be locked and smoke-tested Day 1 — see open item in CLAUDE.md). Transcription runs as an async background job: a status column + polling worker claims pending jobs (`FOR UPDATE SKIP LOCKED`), with stale-row recovery for jobs that die mid-flight — the same pattern already proven in production at VideoAmp/R-EX. Backfill is hard-bounded to the **last 12 months** from build date, enforced in code, not just documented. A small initial batch (2-4 weeks) is transcribed and validated before the full 12-month backfill is allowed to run unattended.
Why: Multi-hour meeting audio can't transcribe synchronously in an HTTP request — same shape of problem the R-EX async worker pattern already solved, reused deliberately. The 12-month bound prevents an open-ended, cost-unbounded backfill.
Date: 2026-08-19

## #13 — Provider: OpenAI for embeddings, transcription, and generation
Decision: Use OpenAI for all three model-dependent pieces this cycle — embeddings, Granicus meeting transcription, and RAG-answer generation. One account, one API key, one bill.
Why: Anthropic (Claude) doesn't offer a public embeddings API or a speech-to-text product — both are OpenAI-only among Amber's existing accounts (Anthropic/Claude and OpenAI/ChatGPT). Generation is the only piece where Claude was actually a live option; going all-OpenAI this cycle minimizes moving parts for a solo 9-day build. Swapping the generation model to Claude later is a small, decoupled change (one phase, doesn't touch embeddings/retrieval/transcription) if revisited.
Date: 2026-08-19
