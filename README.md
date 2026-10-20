# Coalition Grant App

A grant-application tool for the Florida coalition, administered by the Florida Food Policy Council (FLFPC). Each organization keeps its own authorized documents in it. The app helps find funding opportunities, draft each application field from those documents with citations, route drafts through separate writer / reviewer / approver steps, and track what's been submitted and what's owed to funders after an award.

**Status: early.** This repo was repurposed from a civic-records research tool (DECISIONS #158). Today it does the foundation only: upload a document and ask grounded, cited questions about it. Everything else on the list below is still to be built.

## What works today

1. Upload a PDF, DOCX, or TXT (requires the shared upload key, see "Who can reach this").
2. Ask a plain-language question. The system finds the most relevant passages, answers **only** from them, and cites each one.
3. If the documents don't contain the answer, it says so instead of guessing.

## What's planned (DECISIONS #158)

| Phase | What | Why it matters |
|---|---|---|
| B | Logins with MFA (AWS Cognito), organizations, roles, time-limited partner access | Who you are and what you're assigned to |
| C | Each organization's documents walled off from the others, including in search and AI answers | Nothing sensitive goes in until this exists |
| D | Opportunity tracker, master-spreadsheet import/export, applications (LOI linked to full), word/character limits | The core grant workflow |
| E | Field-by-field drafting with citations, review chain, comments, version history, audit log | Writer never approves their own work; every change is recorded |
| F | Readiness checks, Word/package export, deadline reminders | A person submits; the app prepares |

## If you're taking this over, start here

**Why won't it just write from what it "knows"?** Because a grant application with an invented metric or a partner commitment nobody agreed to is worse than a blank field. It can get an organization disqualified or damage a funder relationship. The system only states facts it can point back to in an authorized document, and it flags gaps instead of filling them.

**Who can reach this?** The deployed instance has a public address. Uploads need a shared key (DECISIONS #144), but `/query` and `/sources` are open, and there are no individual logins yet. **Don't upload real partner documents (EINs, financials, banking details, anything non-public) to a deployed instance until Phase C ships.** Phase B adds logins, but only Phase C stops one organization's users from searching another's documents.

**Does it submit applications or send email?** No, and the first version never will. It prepares packages for a designated person to submit.

**Does it store funder-portal passwords?** No. People log into funder portals themselves.

**What does it cost?** Fargate + RDS run 24/7, and OpenAI is billed per call. There's a monthly AWS budget alert (`infra/register_cost_alerts.sh`, DECISIONS #133).

**Open security item:** a credential-looking value was found in AWS IAM tag metadata (DECISIONS #140). Rotating and removing it is still open.

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

Postgres + pgvector and the API (port 8000). UI at `http://localhost:8000/`. Copy `.env.example` to `.env` and fill it in first.

Tests: `pip install -r requirements.txt -r requirements-dev.txt`, start the `db` service, then `pytest`.

## Running it live

AWS Fargate (`api` service) + RDS. Rebuild and push with `infra/build_push_app_image.sh`, then register the task definition with `infra/register_app_taskdefs.sh` and update the ECS service. Read `.claude/rules/secrets.md` before running any command against `.env`.

## Stack

Python 3.12, FastAPI, PostgreSQL + pgvector, Docker, AWS Fargate/ECS + RDS (DECISIONS #1). OpenAI `text-embedding-3-small` and `gpt-4o-mini` (DECISIONS #13). Cognito for auth (planned, DECISIONS #158).
