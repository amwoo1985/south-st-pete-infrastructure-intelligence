# Coalition Grant App

(Repurposed in place from "Blaq Blob" / South St. Petersburg Infrastructure Intelligence — DECISIONS #158. That tool's crawlers, Granicus transcription pipeline, and civic-records corpus were dropped; its grounded-RAG core, upload path, and AWS deploy were kept.)

A multi-tenant fundraising platform for nonprofits, offered as a subscription (DECISIONS #165). Each organization is an isolated tenant with its own logins, documents, application history and funder relationships. The Florida coalition, administered by the Florida Food Policy Council (FLFPC; Erica Hall, initial admin lead), is the first customer, and its requirements still bind. It uses each organization's authorized documents to discover funding opportunities, assess fit, draft application fields one at a time with cited sources, route drafts through separate writer → partner → reviewer → approver steps, and track submissions and award obligations. This is a real operational tool that has to outlast any one person's day-to-day involvement, so whoever inherits it can run it without a live walkthrough (DECISIONS #136).

The requirements source is the "Coalition Grant App — Requirements Questionnaire" (Oct. 6, 2026, Erica's completed draft). Items it marks "Proposed" or "Confirm" are not settled requirements. Don't build on them as if they were; record the working assumption in DECISIONS.md.

## Workflow

Entry point for any build, review, or design-decision task: invoke the `cba-rag` orchestrator agent. It discovers conventions here, delegates to specialists — `rag-pipeline`, `api-layer`, `deploy-infra` for building; `rag-review`, `api-review`, `deploy-review` for adversarial review — synthesizes their output, and owns `DECISIONS.md`.

`.claude/rules/secrets.md` governs any command that touches `.env` or another file carrying a live credential — binding on every specialist and on direct shell commands in this session alike (see DECISIONS #131, #135).

Every non-trivial design decision gets a numbered `DECISIONS.md` entry before or immediately after it's made, never batched at the end.

## Precedence

`CLAUDE.md` (this file) > `.claude/rules/<domain>.md` (project) > specialist agent judgment > general best practice.

## Stack

- Python 3.12, FastAPI, minimal web UI
- PostgreSQL + pgvector (embeddings/retrieval)
- Docker + docker-compose (local), AWS Fargate/ECS + RDS (deploy)
- Embedding/LLM provider: **OpenAI** (`text-embedding-3-small`, `gpt-4o-mini`) — DECISIONS #13. The API does not train on submitted data by default; questionnaire 8.5 still requires checking each funder's and partner's terms before their documents are processed.
- Auth: **AWS Cognito** with MFA (authenticator app / passkeys) — DECISIONS #158. Not built yet (Phase B).

## Phases (DECISIONS #158)

- **A** — Strip the civic-records tool down to its reusable core. ← done in #158
- **B** — Cognito auth + MFA, organizations, users, roles, time-limited per-application access.
- **C** — Per-organization document vault; retrieval isolated per org (no cross-org leaks through search, exports, or AI answers).
- **D** — Opportunities, master-spreadsheet import/export, applications (LOI ↔ full), fields with word/char limits.
- **E** — Field-by-field cited drafting, review/approval chain, comments, version history, audit log.
- **F** — Readiness checks, Word/package export, deadline reminders.

The detailed design plan is kept locally, not committed.

## Hard constraints (do not relitigate without a new DECISIONS.md entry)

- No Kubernetes, no Terraform/IaC this cycle (DECISIONS #2).
- One AWS deploy target (Fargate) (DECISIONS #1).
- **No real partner documents on any deployed instance until Phase C's per-org retrieval scoping and its cross-org leak tests have shipped.** Phase B's logins alone aren't enough: without C, any signed-in user's `/query` searches every org's documents. The deployed instance also needs HTTPS on a stable hostname first (DECISIONS #162): today it's plain http on a changing IP, and Cognito won't redirect to it. Today every API route except `/health` needs a signed-in user (DECISIONS #169), but there is no per-org scoping: any signed-in user's `/query` searches every org's documents, and the deployed instance can't be signed into from a browser until HTTPS exists. That's fine for test data and wrong for EINs, financials, or banking details.
- **The app prepares; the org acts.** It never submits applications and never emails funders on an org's behalf. It drafts, packages, reminds and gives copy-ready answers (questionnaire 7.1, #165). System notifications to the app's own users (deadlines, assignments) are allowed (#159).
- **Every org is a tenant like any other.** No special-casing FLFPC in code. Private data carries `org_id`. Data shared across tenants comes from public sources only (#165).
- **"Learning" is per-org memory, never model training** on client data (questionnaire 8.5, #165).
- **The app never stores funder-portal passwords, recovery codes, or MFA secrets**, and credentials are never available to the AI drafting process (questionnaire 9.3, 9.4).
- **Generated text never invents** achievements, metrics, partner commitments, or future funding. Every factual claim traces to a cited, authorized source; anything without a source gets flagged instead of written (questionnaire 5.4).
- A writer can never approve their own application (questionnaire 2.1).
- Every retrieval/generation path must be explainable in plain language to a non-engineer successor — no undocumented magic.

## Ongoing-operation notes

- Fargate/RDS run 24/7 at real (if small) cost; OpenAI usage scales with drafting volume (working assumption: 5–10 applications/month, questionnaire 3.1).
- The live RDS still holds the old civic-records corpus and the orphaned `crawl_runs` / `granicus_transcription_jobs` tables, and the deployed API image is still the pre-#158 build. See DECISIONS #158 for the AWS cleanup that's waiting on explicit sign-off.
