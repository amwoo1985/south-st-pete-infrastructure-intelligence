# Coalition Grant App

A grant-application tool for the Florida coalition, administered by the Florida Food Policy Council (FLFPC). Each organization keeps its own authorized documents in it. The app helps find funding opportunities, draft each application field from those documents with citations, route drafts through separate writer / reviewer / approver steps, and track what's been submitted and what's owed to funders after an award.

**Status: early.** This repo was repurposed from a civic-records research tool (DECISIONS #158). The foundation (upload a document and ask grounded, cited questions about it) and Phase B (sign-in checks, organizations, roles, invitations, vendor support access, an audit log) are built and tested. They are not deployed yet, and nobody can sign in from a browser until HTTPS exists (DECISIONS #162), so nothing is usable on the live instance. Everything else on the list below is still to be built.

## What works today

1. Upload a PDF, DOCX, or TXT (requires a signed-in account, see "Who can reach this").
2. Ask a plain-language question. The system finds the most relevant passages, answers **only** from them, and cites each one.
3. If the documents don't contain the answer, it says so instead of guessing.

## What's planned (DECISIONS #158)

| Phase | What | Why it matters |
|---|---|---|
| B | **Built, not deployed** (the test suite passed at the last Phase B commit). Cognito token checks (MFA required on the pool), organizations, roles, invitations that must be accepted, consented vendor support access, audit log, a bootstrap tool. Browser sign-in (B5) is waiting on HTTPS. Per-application assignments moved to D. | Who you are and what you're assigned to |
| C | Each organization's documents walled off from the others, including in search and AI answers | Nothing sensitive goes in until this exists |
| D | Opportunity tracker, master-spreadsheet import/export, applications (LOI linked to full), word/character limits | The core grant workflow |
| E | Field-by-field drafting with citations, review chain, comments, version history, audit log | Writer never approves their own work; every change is recorded |
| F | Readiness checks, Word/package export, deadline reminders | A person submits; the app prepares |

## If you're taking this over, start here

**Why won't it just write from what it "knows"?** Because a grant application with an invented metric or a partner commitment nobody agreed to is worse than a blank field. It can get an organization disqualified or damage a funder relationship. The system only states facts it can point back to in an authorized document, and it flags gaps instead of filling them.

**Who can reach this?** The deployed instance has a public address. Every API route except `/health` needs a signed-in user (DECISIONS #169), but browser sign-in isn't built yet, so the deployed instance can't upload or query for now. **Don't upload real partner documents (EINs, financials, banking details, anything non-public) to a deployed instance until Phase C ships.** Phase B decides who may ask, but only Phase C stops one organization's users from searching another's documents.

**How do people get access?** Not by self-sign-up. A platform admin (the vendor) is created with the command-line tool (`python -m app.admin`, DECISIONS #174; run it in production with `infra/run_admin.sh`), which also creates an organization and seats its first admin. After that an organization's admin invites their own staff through the API; the person gets a role only when they accept the invitation (DECISIONS #170-#173). Vendor staff are never members of a customer's organization: they get in only through access the customer's admin grants for at most 14 days, and every such grant is audited (DECISIONS #166). Inviting sends an email through Cognito's built-in sender, which allows 50 emails a day for the whole AWS account, so invitations are capped at 30 a day across all customers (DECISIONS #175).

**Does it submit applications or send email?** No, and the first version never will. It prepares packages for a designated person to submit.

**Does it store funder-portal passwords?** No. People log into funder portals themselves.

**What does it cost?** Fargate + RDS run 24/7, and OpenAI is billed per call. There's a monthly AWS budget alert (`infra/register_cost_alerts.sh`, DECISIONS #133).

**Security items:** the credential-looking IAM tag from DECISIONS #140 is closed (DECISIONS #168). Two IAM roles not created by this project have never been reviewed.

## Architecture

```
Upload (PDF/DOCX/TXT) → chunk (app/chunking/uploaded_document.py)
      → embed + store in pgvector (app/embeddings/, idempotent per chunk)
Question → retrieve (similarity threshold, near-duplicate removal, token cap; app/rag/retrieval.py)
      → generate an answer only from those passages, with citations (app/rag/generation.py)
FastAPI (app/api/): POST /query, GET /health, GET /sources/{doc_id}, POST /documents/upload
Static UI (app/api/static/), served by the same app
```

Every chunk carries its source, its own date where known, and when it was ingested (`app/provenance.py`). Every citation resolves to a real row via `GET /sources/{doc_id}`.

## Running it locally

```
docker-compose up --build
```

Postgres + pgvector and the API (port 8000). UI at `http://localhost:8000/`. Copy `.env.example` to `.env` and fill it in first (including the three `COGNITO_*` values: without them every route except `/health` answers 503).

The API never creates tables itself; it refuses to start if the database is behind. Apply migrations first (and after pulling changes that add one):

```
docker-compose run --rm api python -m app.db.migrate up
```

Tests: `pip install -r requirements.txt -r requirements-dev.txt`, start the `db` service, then `pytest`.

## Running it live

AWS Fargate (`api` service) + RDS. **Follow `infra/DEPLOY_RUNBOOK.md`**: it has the one-time IAM step for the api's Cognito role, the exact order, the first-admin setup, and what you cannot undo. The outline:

1. `infra/build_push_app_image.sh` (build and push the image)
2. `IMAGE_TAG=<sha> infra/register_app_taskdefs.sh` (registers the `api` and `migrate` task definitions; needs the three `COGNITO_*` values in `.env`, which `infra/register_cognito.sh` prints)
3. `infra/run_migration.sh up` (**cannot be undone**: the previous api build refuses the newer schema, so take a snapshot first; see the runbook. Applies schema changes as the database owner; `status` and `report` are read-only. `report` prints uploaded filenames into the CloudWatch log group `/ecs/south-st-pete-app`). If `up` fails with a lock timeout (the live api was mid-query), nothing was changed; just run it again
4. Update the ECS `api` service to the new task definition.

The api logs into RDS as a limited `app_runtime` role that can read and write rows but can't change the schema. Only the one-off migrate task uses the master credentials (DECISIONS #160). Read `.claude/rules/secrets.md` before running any command against `.env`.

## Stack

Python 3.12, FastAPI, PostgreSQL + pgvector, Docker, AWS Fargate/ECS + RDS (DECISIONS #1). OpenAI `text-embedding-3-small` and `gpt-4o-mini` (DECISIONS #13). Cognito for auth (token verification and invitations built; browser sign-in waits on HTTPS).
