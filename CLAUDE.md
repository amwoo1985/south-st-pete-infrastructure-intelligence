# Blaq Blob

(Formerly "cba-rag-assistant," then "South St. Petersburg Infrastructure Intelligence" — renamed per DECISIONS #9, then DECISIONS #136.)

A RAG tool over local government meeting records, grants, and (later) statutes — built for Amber Woods's real, ongoing civic work chairing the South St. Petersburg Energy Coalition and its Community Benefits Agreement negotiation with the City of St. Petersburg, residents, and Duke Energy Florida. This is a real operational tool, not a portfolio demo — built to outlast Amber's own day-to-day involvement, so whoever she hands it to next can run it without her walking them through it live (DECISIONS #136).

## Two independent tracks — do not conflate

1. **CV + AI self-analysis** — due 2026-08-26 12PM. Handled entirely outside this project. Not scheduled here, not gated by this project's progress.
2. **This project** — its own 9-day window, Day 1 = 2026-08-19, Day 9 = 2026-08-27. Not gated by the CV/analysis deadline. See DECISIONS #9.

Conflating these two was the exact ambiguity that cost ~2 months on R-EX at VideoAmp. Keep them separate.

## Workflow

Entry point for any build, review, or design-decision task: invoke the `cba-rag` orchestrator agent. It discovers conventions here, delegates to specialists — `rag-pipeline`, `api-layer`, `deploy-infra`, `crawler` for building; `rag-review`, `api-review`, `deploy-review`, `crawler-review` for adversarial review — synthesizes their output, and owns `DECISIONS.md`.

`.claude/rules/secrets.md` governs any command that touches `.env` or another file carrying a live credential — binding on every specialist and on direct shell commands in this session alike (see DECISIONS #131, #135).

Every non-trivial design decision gets a numbered `DECISIONS.md` entry before or immediately after it's made, never batched at the end.

This mirrors the agentic workflow used to build R-EX at VideoAmp (orchestrator + narrow specialists + adversarial reviewers + a numbered decisions log), sized for a solo real-tool build instead of an enterprise monorepo.

## Precedence

`CLAUDE.md` (this file) > `.claude/rules/<domain>.md` (project) > specialist agent judgment > general best practice.

## Stack

- Python 3.12, FastAPI, minimal web UI
- PostgreSQL + pgvector (embeddings/retrieval)
- Docker + docker-compose (local), AWS Fargate/ECS + RDS (deploy)
- Embedding/LLM provider: **OpenAI** (`text-embedding-3-small`, `gpt-4o-mini`) — DECISIONS #13, smoke-tested 2026-08-19 with a real embedding call and chat completion.
- Hosted transcription API provider: **OpenAI** (`whisper-1`) — DECISIONS #13, smoke-tested 2026-08-19 against a real St. Petersburg City Council meeting clip.

## Data sources (DECISIONS #11, #12)

- **Tier 1** (build first): Pinellas County BCC via Legistar; St. Petersburg grants/loans; Pinellas Community Foundation grants.
- **Tier 1.5** (own sub-pipeline): St. Petersburg City Council via Granicus — RSS-indexed by date, direct MP3 per meeting, hosted-API transcription as an async worker job, 12-month hard backfill bound.
- **Deferred, not built this cycle**: Florida PSC (needs headless-browser investigation), GovTrack (blocked — needs real API investigation), Florida Statutes (not a crawler target — separate on-demand citation-lookup tool).
- Adding any new source requires a new DECISIONS.md entry first.

## Plan

See `PLAN.md` for the phase breakdown, day-by-day schedule, and checkpoint/fallback order. (PLAN.md's original "interview-defense" framing is historical — see DECISIONS #136; README's handoff-notes section is the live reference now.)

## Hard constraints (do not relitigate without a new DECISIONS.md entry)

- No Kubernetes, no Terraform/IaC this cycle (DECISIONS #2).
- One AWS deploy target (Fargate), decided once (DECISIONS #1).
- Every retrieval/generation path must be explainable in plain language to a non-engineer successor — no undocumented magic.
- Crawler source list is closed (DECISIONS #11) — no silent scope expansion.
- Granicus backfill is hard-bounded to 12 months (DECISIONS #12).

## Ongoing-operation notes (this is not a one-off demo)

- Every crawler breaks when its source site's structure changes — needs at least basic visibility (a health/status view), not silent failure.
- Recrawl cadence per source is not yet decided — needs its own DECISIONS entry once crawlers are stable.
- Fargate/RDS run 24/7 at real (if small) cost; transcription API usage across 12 months of multi-hour audio is likely the single largest cost line — set a budget alert before the unattended full backfill runs.
- The live upload endpoint has no auth yet (explicitly a stretch item) — do not upload actually-sensitive negotiation documents to a publicly reachable deployed instance until that's addressed.
