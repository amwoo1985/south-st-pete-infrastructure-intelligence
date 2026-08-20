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

## #14 — HTTP client library for crawlers: `requests`, not `httpx`
Decision: All crawler HTTP fetching (`app/crawlers/base.py`) uses the synchronous `requests` library.
Why: `requests` was already present and working in the project `.venv`; an attempted `httpx` install resolved to a broken package in this environment (`import httpx` failed). Crawlers here run as sequential, rate-limited, one-request-at-a-time jobs against a handful of government sites — there's no concurrency workload that would justify `httpx`'s async client. Simplicity over a marginal capability this project doesn't need.
Date: 2026-08-20

## #15 — Crawler rate-limit interval: 2.0 seconds minimum per host
Decision: `app/crawlers/base.py`'s `RateLimiter` enforces a minimum 2.0-second gap between requests to the same host by default, real `time.sleep`-based throttling, not a documented convention.
Why: None of the Tier-1/1.5 sources publish a documented crawl-delay in `robots.txt` (Legistar has no `robots.txt` at all — confirmed via a live fetch returning HTTP 404 during Day 2 recon). Absent a stated rate, 2.0s is a conservative, clearly-non-hammering default for a small government site's infrastructure, cheap to raise later if a source proves more tolerant or needs to be slower.
Date: 2026-08-20

## #16 — robots.txt verification: stdlib `urllib.robotparser`, fail loud when unverifiable
Decision: `RobotsChecker` in `app/crawlers/base.py` fetches and parses `robots.txt` per host using Python's built-in `urllib.robotparser.RobotFileParser`, through the same rate-limited, honestly-user-agented request path as every other fetch. A `404` on `robots.txt` is treated as "no restrictions declared" (standard convention). Any other fetch failure (timeout, 5xx, etc.) raises `RobotsError` instead of defaulting to "allowed."
Why: `robotparser` is stdlib — no new dependency for a well-solved, standard problem. Distinguishing "confirmed no restrictions" from "couldn't verify" matters: silently treating a fetch failure as permission would violate the "check it, don't assume" rule in `.claude/rules/crawler.md` even though it's a narrow edge case.
Date: 2026-08-20

## #17 — Closed source list enforced in code, not just by convention
Decision: `app/crawlers/base.py` defines `ALLOWED_SOURCE_HOSTS`, mirroring DECISIONS #11's Tier-1/Tier-1.5 hosts exactly. `BaseCrawler.fetch()` raises `ScopeViolationError` before any request whose host isn't in that set.
Why: `.claude/rules/crawler.md` states its job is to make silent scope creep "structurally awkward, not just discouraged." A code-level allow-list checked on every fetch — rather than trusting each crawler module to only ever call the right URLs — is the structural version of that rule, and gives `crawler-review` a single place to verify DECISIONS #11 compliance instead of auditing every call site.
Date: 2026-08-20

## #18 — Attribution metadata shape: frozen dataclass, `published_date` nullable
Decision: `app/crawlers/base.py`'s `Attribution` dataclass carries `source_url: str`, `retrieval_timestamp: datetime` (always set, UTC, tz-aware, non-nullable), and `published_date: date | None = None`.
Why: `.claude/rules/crawler.md` mandates all three fields on every item; `.claude/rules/data.md` mandates nullable-over-sentinel for values not yet known at capture time. `retrieval_timestamp` is always knowable the instant a fetch happens, so it's required. `published_date` (the meeting/effective date) depends on a source's parse succeeding, so it's `Optional` rather than defaulting to a magic placeholder like `1970-01-01`.
Date: 2026-08-20

## #19 — HTML parsing library: BeautifulSoup4 + lxml
Decision: `app/crawlers/legistar.py` parses Legistar's calendar table and Accessible-Agenda HTML pages using `beautifulsoup4` with the `lxml` parser backend.
Why: Legistar's markup is old-style ASP.NET WebForms output (nested `<font>` tags, `<table>`-based layout, verbose auto-generated element IDs) — BeautifulSoup's tolerant parsing handles that without hand-rolled regex-on-HTML, which is brittle and exactly the kind of thing that silently breaks instead of failing loud. `lxml` is used as the parser backend for speed and stricter standards conformance than the stdlib `html.parser`. Neither was previously in the project `.venv`; both were installed and pinned in `requirements.txt` this session.
Date: 2026-08-20

## #20 — PDF text extraction library: pypdf
Decision: `app/crawlers/legistar.py`'s PDF-fallback path (used when Legistar doesn't offer an Accessible-Agenda HTML view for a meeting) extracts text via `pypdf`.
Why: Pure-Python, no external binary dependency (unlike `pdftotext`/poppler), actively maintained, and sufficient for Legistar's agenda PDFs, which are text-layer PDFs (not scanned images) — confirmed by extracting real text from a live agenda PDF during Day 2 recon. OCR is explicitly out of scope; if a future agenda PDF turns out to be a scanned image with no text layer, `_extract_pdf_text`'s empty-text check raises `CrawlerStructureError` rather than silently returning nothing.
Date: 2026-08-20

## #21 — Legistar "not viewable by the public" meetings are captured, not skipped or fail-loud
Decision: When a calendar row's Meeting Details cell has no link (Legistar renders these with a `meeting_NotViewable` class and no `href`, e.g. certain closed/executive-session items), `app/crawlers/legistar.py` records the meeting with `meeting_detail_url=None` and falls back to the calendar page itself as the item's `source_url`. It does not raise `CrawlerStructureError` and does not drop the row.
Why: This is a real, expected Legistar state (confirmed live in Day 2 recon), not a sign of a broken parser — treating it as a structure failure would make the crawler fail loud on legitimate data. Dropping the row silently would violate the "never silently return empty" rule from a different angle: the meeting exists on the calendar and its occurrence (date, body, time) is itself attributable information even when its detail page isn't public.
Date: 2026-08-20

## #22 — Legistar past-meeting querying: replicate the calendar's ASP.NET postback, not a query string
Decision: `LegistarCrawler.crawl()` gained a `date_range` parameter (default `"This Month"`, matching the previous behavior exactly). A non-default value (e.g. `"2025"`) routes through a new `_fetch_calendar_for_date_range()`, which GETs `Calendar.aspx` once to capture the page's current `__VIEWSTATE`/`__EVENTVALIDATION`/every other control value, overrides the "Date Range Dropdown List" (`lstYears`) fields to the requested value, and POSTs that full field set back with the "Search Calendar" button field included — replicating what a real browser submission of that Telerik RadComboBox + button click produces. `_verify_date_range_applied()` then checks the response actually echoes the requested value back in `lstYears_Input` before returning it, and fails loud if not — a hardcoded field-name postback that silently no-ops (e.g. if Legistar renames the control IDs) would otherwise return real, well-formed, but wrong-range data with no error, which is worse than an empty result. Live-verified 2026-08-20: querying `date_range="2025"` against the real site returned 52 meetings, 43 of them with a populated `minutes_pdf_url` resolving to a real `application/pdf` response (confirmed by fetching one: `View.ashx?M=M&ID=1249432...`, 200, `application/pdf`, ~1.26MB).
Why: Legistar's calendar has no `Calendar.aspx?Year=2025`-style GET parameter for the date-range filter — recon confirmed the control is a Telerik RadComboBox inside a WebForms postback, so any past-meeting query has to be a real postback, not a URL hack. This was also the only way to close the Day 2 self-review gap: the default "This Month" view, fetched during Day 2, had zero meetings with posted minutes (nothing old enough had happened yet), so `_cell_link_url`'s minutes-column extraction had code but no real-data proof. Default behavior for existing/future callers is unchanged — `date_range="This Month"` still does the same plain GET as before.
Date: 2026-08-20

## #23 — Crawler test suite: pytest + `responses`, recorded live fixtures, no network at test time
Decision: Added `tests/` (pytest suite) covering `app/crawlers/base.py` and `app/crawlers/legistar.py`. HTTP-dependent tests mock `requests` traffic with the `responses` library rather than hitting the live site; fixtures under `tests/fixtures/legistar/` (`calendar_this_month.html`, `calendar_2025.html`, `accessible_agenda.html`, `agenda_fallback.pdf`, `minutes_sample.pdf`) were recorded once this session by running the real crawler code (`LegistarCrawler.fetch()`/`_fetch_calendar_for_date_range()`) against the live site and saving the responses verbatim — not hand-written or synthesized. Structure-failure (fail-loud) tests use small hand-built synthetic HTML instead, since there's no live "broken" page to record. Rate-limiter timing is tested against a monkeypatched `time.monotonic`/`time.sleep`, not a real 2-second wait. `pytest` and `responses` added to `requirements.txt`, pinned to the versions installed and verified in `.venv` this session (`pytest==9.1.1`, `responses==0.26.2`). A `pytest.ini` sets `pythonpath = .` so tests import `app.*` without an editable install.
Why: `.claude/rules/crawler.md`'s fail-loud and attribution rules are only worth anything if they're actually verified, and the prior agent's own self-report named "no automated test suite" as an open gap. Recorded fixtures make the suite deterministic and fast (full run: 22 tests in under 2s) and immune to the live site changing or blocking between runs, while still being real data rather than guessed-at markup — the same "verify against reality, then freeze it" pattern used to build DECISIONS #19/#20's parsing choices in the first place.
Date: 2026-08-20

## #24 — Fixed broken `httpx`/`httpcore` install; added real `httpx` to requirements.txt for FastAPI's TestClient (Phase H)
Decision: `.venv` had non-existent packages `httpx2`/`httpcore2` installed (a prior mis-specified install), which left `import httpx` failing. Uninstalled both and installed the real `httpx` package (`0.28.1`), verified `import httpx` succeeds. Added `httpx==0.28.1` to `requirements.txt`.
Why: DECISIONS #14 already settled the crawler HTTP client question (`requests`, not `httpx`) and that stands unchanged — this entry doesn't reopen it. But `PLAN.md` Phase H is FastAPI, and FastAPI's `TestClient` depends on a working `httpx` install regardless of what the crawlers use internally. Fixing this now, while `requirements.txt` was already being touched for the pytest/`responses` addition (DECISIONS #23), avoids the same broken-package trap resurfacing when Phase H starts.
Date: 2026-08-20

## #25 — Scanned/no-text-layer PDF test fixture is synthetic, not a found live example
Decision: `tests/fixtures/legistar/agenda_scanned_no_text.pdf` is a hand-built single-page PDF: a page of text was rasterized onto a bitmap image (via PIL) and that image alone was embedded into a PDF page (via reportlab) with no PDF text objects at all, so `pypdf`'s `extract_text()` legitimately returns an empty string. `pypdf`/`reportlab` were installed only transiently to build this one fixture file and were uninstalled afterward — neither is a runtime dependency and neither is in `requirements.txt`.
Why: A bounded live-corpus search — both `agenda_pdf_url` and `minutes_pdf_url` documents sampled across five years (2015, 2018, 2020, 2022, 2024; 20 PDFs total, fetched through the real crawler) — found every single one was a genuine text-layer PDF, none scanned/image-only. That's consistent with DECISIONS #20's original finding. Rather than spend more of the session searching an apparently-clean corpus for a needle that may not exist, a synthetic fixture proves the fail-loud path actually fires on a real no-text-layer PDF structure, honestly labeled as constructed rather than found.
Date: 2026-08-20

## #26 — stpete.org grants/loans crawler fetches the bare `stpete.org` URL named in DECISIONS #11, unchanged
Decision: `app/crawlers/stpete_grants.py`'s `GRANTS_URL` constant is `https://stpete.org/residents/grants___loans/index.php` — the exact bare-domain URL named in DECISIONS #11 — even though live recon (2026-08-20) confirmed the site actually serves content from `www.stpete.org` (the bare domain 301-redirects: `stpete.org` → `http://www.stpete.org/...` → `https://www.stpete.org/...`). `ALLOWED_SOURCE_HOSTS` in `app/crawlers/base.py` is left unchanged (still `stpete.org`, not `www.stpete.org`).
Why: `requests` follows redirects transparently by default, so `BaseCrawler.fetch()`'s scope check (which inspects the *requested* URL's host, before any redirect) and its per-host robots.txt/rate-limit bookkeeping both key off `stpete.org` and work correctly without modification — the redirect is invisible to the caller except in the final response's already-resolved content. No code or scope-list change was needed; this entry exists so a future reader isn't surprised to see `www.stpete.org` show up in fetched HTML (e.g. in the page's `<base>` tag, DECISIONS #28) despite the crawler only ever requesting the bare domain.
Date: 2026-08-20

## #27 — stpete.org grants/loans: index page only, category sub-pages flagged as a blocker, not followed
Decision: `app/crawlers/stpete_grants.py` crawls exactly `https://stpete.org/residents/grants___loans/index.php` and extracts its 6 category tiles (name, link, optional caption) as `StpeteGrantCategory` items with `published_date=None`. It does not fetch any category sub-page (e.g. `residents/grants___loans/business.php`), even though those sub-pages are linked directly from the index page and themselves link to further sub-pages (e.g. `for_business_owners.php`) that appear to hold the actual per-program grant details (amounts, deadlines, effective dates).
Why: DECISIONS #11 names an exact page, not a domain or a directory tree, and the task instructions for this session were explicit: if the target page's structure suggests related content elsewhere on the domain, flag it as a blocker rather than follow it. Recon confirmed the index page itself carries no per-item dates or amounts — only category names and links — so the closed-scope crawl here produces attributable but thin data (category directory only). **Blocker for a future DECISIONS entry:** if per-program grant details (dates/amounts/eligibility) are wanted from stpete.org, the actual content lives at least two directory levels deep from the named index page and needs its own explicit URL list and DECISIONS entry before any crawler follows it.
Date: 2026-08-20

## #28 — stpete.org tile links resolved against the page's declared `<base>`, not the fetched URL
Decision: `app/crawlers/stpete_grants.py`'s `parse_index()` reads the page's `<base href="...">` tag (confirmed live: `<base href="https://www.stpete.org/" />`) and resolves every tile's relative `href` against that base, falling back to `GRANTS_URL` only if a future revision of the page drops the `<base>` tag entirely.
Why: The page's tile hrefs are site-root-relative (e.g. `residents/grants___loans/business.php`), written on the assumption that the browser applies the declared `<base>`. Resolving them against the fetched page URL's own directory instead (`urljoin(GRANTS_URL, href)`) was tried first during this session and confirmed live to silently produce a doubled, broken path (`.../grants___loans/residents/grants___loans/business.php`) — exactly the kind of "well-formed but wrong" failure DECISIONS #22 warns about, not caught by any fail-loud check because the URL is syntactically valid. Reading the real `<base>` tag is the correct general fix rather than a one-off path hack.
Date: 2026-08-20

## #29 — Pinellas CF AWARD DISTRIBUTION date parsing: nullable on no-match, not fail-loud
Decision: `app/crawlers/pinellas_cf.py`'s `_parse_award_date()` extracts a first-of-month `date` from the grants table's free-text "AWARD DISTRIBUTION" cell (e.g. "March 2026", "Early December 2026") via a `Month YYYY` regex search. If no match is found, `published_date` is set to `None` rather than raising `CrawlerStructureError`.
Why: All 7 rows observed live (2026-08-20) matched the pattern, but the cell is free text editable independently of the table's HTML structure (unlike a missing `<td>` or a renamed column header, which are structural changes and do stay fail-loud in `parse_grants_table`/`_parse_row`). A future row with a genuinely unannounced distribution date (e.g. "TBD") is a legitimate "not yet known" content state, the same category of case as DECISIONS #21's Legistar "not viewable by the public" meetings — nullable per `.claude/rules/data.md`, not a parser break.
Date: 2026-08-20
