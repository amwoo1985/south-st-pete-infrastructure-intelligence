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

## #30 — stpete.org scope expanded to the 6 named category sub-pages; supersedes #27's blocker
Decision: The stpete.org source (DECISIONS #11) is expanded from the single index page to exactly these 6 additional named URLs, confirmed live via the index crawler itself on 2026-08-20:
- `https://www.stpete.org/residents/grants___loans/business.php` (For Business)
- `https://www.stpete.org/residents/grants___loans/community.php` (For Community & Neighborhoods)
- `https://www.stpete.org/residents/grants___loans/housing.php` (For Housing)
- `https://www.stpete.org/residents/grants___loans/for_south_stpete.php` (For South St. Pete CRA)
- `https://www.stpete.org/residents/grants___loans/youth.php` (For Youth)
- `https://www.stpete.org/residents/grants___loans/sunrise_st._pete/index.php` (Sunrise St. Pete)

A future crawler task may fetch these 6 pages and extract per-program grant details (amounts, deadlines, eligibility). **This entry does NOT pre-authorize going any deeper.** DECISIONS #27's recon noted at least one of these sub-pages (`business.php`) appears to be itself a hub linking to further pages (e.g. `for_business_owners.php`). If real per-program data turns out to live at a third directory level rather than on these 6 pages directly, that requires its own DECISIONS entry naming the further URLs — not an assumption that this entry's scope grant extends that far.
Why: Amber reviewed DECISIONS #27's blocker and chose to expand scope now rather than leave the crawler's output thin — the 6 index-level category tiles carry no dates/amounts, so a query against them can't answer "what grants exist and when." Naming the exact 6 URLs (not "stpete.org's grants section" generally) keeps the same discipline as #11/#17: closed, finite, code-enforceable scope, not an open mandate to crawl anything reachable from the index page.
Date: 2026-08-20

## #31 — `www.stpete.org` added to `ALLOWED_SOURCE_HOSTS`
Decision: `app/crawlers/base.py`'s `ALLOWED_SOURCE_HOSTS` now includes `www.stpete.org` alongside the existing bare `stpete.org` entry.
Why: DECISIONS #30 names the 6 category sub-page URLs with the `www.` host explicitly (e.g. `https://www.stpete.org/residents/grants___loans/business.php`) — these are the literal resolved URLs the index-page crawler produces after applying the page's `<base href="https://www.stpete.org/" />` tag (DECISIONS #28), not a host a future crawler derives on its own. `BaseCrawler.fetch()`'s scope check inspects the *requested* URL's host before any redirect (DECISIONS #26), so fetching these exact `www.stpete.org` URLs directly — rather than deriving and requesting bare-domain equivalents that would then redirect — requires the host to be allow-listed explicitly. This is additive only; the existing bare `stpete.org` entry for DECISIONS #11's index page is unchanged.
Date: 2026-08-20

## #32 — 4 of the 6 DECISIONS #30 pages (business/community/housing/youth) confirmed pure hub pages; real per-program data is a third directory level down, not followed
Decision: `app/crawlers/stpete_grant_categories.py`'s `crawl_hub_pages()` fetches `business.php`, `community.php`, `housing.php`, and `youth.php` and extracts each page's category tiles (name, url, optional caption) via `StpeteGrantsCrawler.parse_tiles_page()` — the same `div.v2-tiles-con`/`div.v2-tile` parser DECISIONS #27/#28 built for the index page, reused rather than duplicated because live recon (2026-08-20) confirmed the markup is byte-for-byte the same template. No sub-page beyond these 4 is fetched, even though every tile on every one of these 4 pages links to a further page (e.g. `for_business_owners.php`, `arts_grants_program.php`, `affordable_housing_lot_disposition_program.php`, `community_impact_summer_enhancement_grant.php`) that appears to hold the actual per-program grant details.
Why: DECISIONS #30 explicitly does not pre-authorize going deeper than its named 6 URLs. Recon confirmed all 4 of these pages carry only category names and links — no per-item description (every `v2-tile-caption` observed was empty across all 26 tiles on these 4 pages combined), no dates, no amounts — the closed-scope crawl here produces attributable but thin data, same shape as DECISIONS #27's original single-page finding, now confirmed across 4 of the 6 pages rather than just `business.php`. **Blocker for a future DECISIONS entry:** if per-program grant details are wanted for these 4 categories, the real content lives at a third directory level and needs its own explicit URL list and DECISIONS entry before any crawler follows it — not assumed in scope here.
Date: 2026-08-20

## #33 — `for_south_stpete.php` parsed as freeform `<h3>`/`<h4>`-headed program blocks, not tiles
Decision: `app/crawlers/stpete_grant_categories.py`'s `parse_south_stpete_page()` parses `for_south_stpete.php` as a flat sequence of program blocks inside the `#post .module-container` content region (confirmed live to match exactly once, containing the page's one `<h1>`): each `<h3>` (or occasionally nested `<h4>`, e.g. "Rapid Roof Replacement Program" under the Facade Improvement Grant block) starts a new `StpeteGrantProgramDetail`, its following `<p>` paragraphs accumulate as `description` (paragraphs that are purely a "More Info"/application link, marked by a `<span class="btn">` wrapper, are excluded from `description` and instead contribute their `<a>` href(s) to `detail_urls`), and each block ends at the next heading or `<hr>`. `published_date` is `None` for every item — no dollar amount or deadline text appears anywhere on the page.
Why: Live recon confirmed this page carries no `div.v2-tiles-con` tile grid at all — the only one of the 6 DECISIONS #30 pages with genuine freeform per-program content directly on the page rather than link-only tiles or a further hub. Splitting on heading level (not just `<h3>`) was necessary because the live page contains two real, separately-named sub-programs at `<h4>` rather than `<h3>` — "Rapid Roof Replacement Program" (nested under the Facade Improvement Grant block) and "CRA Developer Incentive Program" (nested under the Affordable Housing Redevelopment Loan block) — treating only `<h3>` as a program boundary would have silently merged each pair's text into one item. 7 total program items were confirmed live: 5 at `<h3>` and 2 at `<h4>`. Excluding link-only paragraphs from `description` avoids duplicating the "More Info on X" link text as if it were program description prose. `published_date=None` is nullable per `.claude/rules/data.md`, not a sentinel — a future page revision that adds real dates would need no schema change.
Date: 2026-08-20

## #34 — `sunrise_st._pete/index.php` parsed as tiles-with-embedded-detail, a third distinct shape among the 6 DECISIONS #30 pages
Decision: `app/crawlers/stpete_grant_categories.py`'s `parse_sunrise_page()` parses the page's 5 "Active Programs" tiles using the same `div.v2-tiles-con`/`div.v2-tile` selectors as the 4 hub pages (DECISIONS #32), but additionally extracts each tile's 2 extra `<p>` elements beyond the (still-empty) `.v2-tile-caption`: a `<p><em>...</em></p>` classified as `eligibility`, and a plain `<p>` classified as `description`. Both are `None` for a tile that omits one (nullable, not a fail-loud condition) — DECISIONS #32 already established that a caption-only tile is a legitimate stpete.org state, not necessarily broken markup. The page's page-level "$159.8 million in federal funding" total and its FAQ prose are not extracted as structured data — they're page-level context, not a per-program figure.
Why: Live recon confirmed all 5 tiles share this exact extra-paragraph shape, unlike the 4 hub pages where every tile's markup stops at the caption. Reusing the existing tile-container/tile selectors (rather than a third from-scratch parser) keeps the DECISIONS #27/#28/#32 tile-grid handling as the one source of truth for that part of the markup; only the "what's inside `.v2-tile-info`" extraction differs. Classifying by `<em>` presence rather than paragraph position/count is robust to a tile that has only one extra paragraph (eligibility or description, not necessarily both) without a structural guess about ordering.
Date: 2026-08-20

## #35 — stpete.org scope expanded a third directory level: 22 named per-program pages linked from the 4 DECISIONS #32 hub pages
Decision: The stpete.org source is expanded again, this time to the specific per-program pages that DECISIONS #32's 4 hub pages (business/community/housing/youth) link to. Confirmed live on 2026-08-20 via the hub-page crawler itself. Named, in full:

From `business.php` (3):
- `https://www.stpete.org/residents/grants___loans/for_business_owners.php`
- `https://www.stpete.org/residents/grants___loans/for_developers.php`
- `https://www.stpete.org/residents/grants___loans/for_property_owners.php`

From `community.php` (8 of 9 — see exclusion below):
- `https://www.stpete.org/residents/grants___loans/arts_grants_program.php`
- `https://www.stpete.org/residents/grants___loans/community_food_grant_program.php`
- `https://www.stpete.org/residents/grants___loans/individual_artist_grant.php`
- `https://www.stpete.org/residents/grants___loans/level_up_arts_grant.php`
- `https://www.stpete.org/residents/grants___loans/mayors_neighborhood_mini-grant_program.php`
- `https://www.stpete.org/residents/grants___loans/mlk_communities_in_action_mini-grant_program.php`
- `https://www.stpete.org/residents/grants___loans/neighborhood_partnership_matching_grants.php`
- `https://www.stpete.org/residents/grants___loans/social_action_funding.php`
- `https://www.stpete.org/residents/grants___loans/stormwater_utility_fee_credits.php`

From `housing.php` (7 of 9 — see exclusions below):
- `https://www.stpete.org/residents/housing/developers/affordable_housing_lot_disposition_program.php`
- `https://www.stpete.org/residents/housing/developers/consolidated_plan.php`
- `https://www.stpete.org/residents/grants___loans/purchase_assistance_program.php`
- `https://www.stpete.org/residents/housing/homeowners/housing_rehabilitation_assistance_program.php`
- `https://www.stpete.org/residents/grants___loans/multi-family_rental_loan_program.php`
- `https://www.stpete.org/residents/grants___loans/rebates_for_affordable_residential_rehabs.php`
- `https://www.stpete.org/residents/sustainability/solar.php`

From `youth.php` (4):
- `https://www.stpete.org/residents/grants___loans/community_impact_summer_enhancement_grant.php`
- `https://www.stpete.org/residents/grants___loans/education_youth_opportunity_grants.php`
- `https://www.stpete.org/residents/grants___loans/youth_development_grants.php`
- `https://www.stpete.org/government/initiatives___programs/youth_opportunity_grants.php`

**Explicitly excluded, not part of this entry's grant:**
- `housing.php`'s links to `for_south_stpete.php` and `sunrise_st._pete/index.php` — these are DECISIONS #30/#33/#34's pages, already crawled; not duplicated as new targets.
- `community.php`'s "Police Forfeiture Grants Program" link to `https://police.stpete.org/forfeitureGrantProgram/` — a different subdomain (`police.stpete.org`, not `www.stpete.org`). Per DECISIONS #11's own "named and finite" logic, a different subdomain is a conscious new-source decision, not a silent inclusion. Deferred; needs its own future DECISIONS entry if wanted, same as PSC/GovTrack in #11.

**This entry does not pre-authorize a fourth directory level.** If any of these 22 pages turn out to be further hub pages rather than the actual program-detail content, flag as a blocker per the same DECISIONS #30 pattern — do not follow further links without another explicit entry.

Why: Amber asked for a 4th round specifically to recover real per-program data (amounts, deadlines, eligibility) that DECISIONS #32 found missing from the 4 hub pages. Enumerating the full, actual link set per hub page (rather than "crawl whatever business.php links to") keeps the same closed/finite/code-enforceable discipline as #11/#17/#30 — the crawler's `ALLOWED_SOURCE_HOSTS`/URL check can be built against this exact list, and any drift (a hub page gaining a new tile later) is a visible, reviewable diff against this entry, not silent scope creep.
Date: 2026-08-20

## #36 — DECISIONS #35 count discrepancy flagged; `for_business_owners.php` confirmed a further (4th-level) hub, not followed
Decision: Two findings from live implementation of DECISIONS #35, both handled without expanding scope:

1. DECISIONS #35's own text is internally inconsistent about the community.php group's count: its title says "22 named per-program pages" and its `community.php` line reads "(8 of 9 — see exclusion below)", but the "Named, in full" bullet list under that line enumerates 9 URLs, not 8. The 9-URL bullet list is arithmetically consistent with the already-recorded `community.html` fixture (10 tiles, confirmed live in DECISIONS #32) minus the 1 excluded Police Forfeiture Grants Program link = 9 — so the bullet list, not the "8 of 9" parenthetical, is treated as the entry's real grant. This makes the actual total 23 URLs, not 22. Every one of those 23 URLs was still fetched and is still exactly what DECISIONS #35 named — no URL outside its bullet lists was touched. This is flagged here, not silently resolved, for Amber to correct the entry's own arithmetic if the 22 count was intentional and a URL should instead be dropped.
2. Of the 23 URLs, `for_business_owners.php` (one of `business.php`'s 3) was confirmed live to be itself another hub page — the same `div.v2-tiles-con`/`div.v2-tile` tile grid as the DECISIONS #32 hub pages, linking to 3 further pages (`grow_smarter.php`, `business/legacy_business_program.php` — note the different `business/` path prefix, not `residents/grants___loans/` — and `tax_incentives.php`). Per DECISIONS #35's own explicit non-authorization of a fourth directory level, these 3 links are captured as link-only tiles (`StpeteProgramDetailsCrawler.crawl_further_hub_page()`, reusing `StpeteGrantsCrawler.parse_tiles_page()` per DECISIONS #32's precedent) but never fetched. Blocker: a fourth-level DECISIONS entry naming these 3 URLs (plus checking whether any of the other 22 detail pages conceal a similar further-hub link inside their prose, which this crawl does not check) would be needed before crawling deeper.

Why: The count mismatch is exactly the kind of drift-from-the-entry the task's own instructions warn about, so it's named explicitly rather than quietly picking whichever number is convenient. The further-hub finding is the same DECISIONS #27/#30/#32 hard-stop pattern applied one level deeper — stpete.org's grants template nests hub pages more than one level deep in at least this one case, so "confirm live before assuming a named URL is a content page" stays the standing practice for any future round.
Date: 2026-08-20

## #37 — `app/crawlers/stpete_program_details.py`: one generic h2-sectioned parser for all 22 detail pages, no multi-program/single-program split
Decision: `StpeteProgramDetailsCrawler.parse_program_detail_page()` parses all 22 DECISIONS #35 detail pages (per DECISIONS #36's corrected count) with a single parser producing one `StpeteProgramDetailPage` per URL: an `<h1>` page title (inside the same `#post .module-container` region DECISIONS #33 confirmed), an optional intro (prose between `<h1>` and the first `<h2>`), and an ordered tuple of `StpeteProgramSection` — one per `<h2>`, each carrying its own paragraph/list/table text, 0+ nested `StpeteProgramSubsection` per `<h3>` (a rare nested `<h4>` folds into its parent `<h3>`'s text, prefixed by the `<h4>`'s own heading text, since only 2 of the 22 pages use `<h4>` at all and always as a one-off detail under an `<h3>`), and the deduplicated hrefs found anywhere in that `<h2>` block.

Live recon confirmed the `<h2>` level does not mean the same thing across all 22 pages: on most (e.g. `individual_artist_grant.php`, `purchase_assistance_program.php`) each `<h2>` is a generic section label (`Overview`, `Eligibility`, `How To Apply`, `Documents`, ...) for the one program the page is about. On a few (`business/for_developers.php` — 3 `<h2>`s naming 3 distinct programs; `business/for_property_owners.php` — 13; `solar.php` — 2 named sub-programs "Solar Co-Ops"/"Switch Together Program"; `stormwater_utility_fee_credits.php` — 2 named credit types) each `<h2>` instead names a separate, complete program/credit/RFP-round in its own right. No reliable structural signal distinguishes the two cases without guessing: `<h1>` text does not reliably predict it either way (`solar.php`'s `<h1>` "Where the Sun Shines" and `for_developers.php`'s `<h1>` "For Developers" both read like ordinary program names, and several genuinely single-program pages have equally generic-sounding `<h1>`s, e.g. `affordable_housing_lot_disposition_program.php`'s `<h1>` "Growing the City Forward"). Rather than build two dataclasses and force every page into "1 program" or "N programs" — getting it wrong on an ambiguous page like `multi-family_rental_loan_program.php` (3 `<h2>`s that could be read as 3 RFP rounds of one program, or 3 distinct RFPs) — `StpeteProgramSection` stores every `<h2>`'s heading text verbatim and lets a caller judge, rather than the crawler asserting a semantic distinction it cannot verify from markup alone.

A `<p>`/`<ul>`/`<ol>`/`<table>` nested inside another matched block element (confirmed live: `mlk_communities_in_action_mini-grant_program.php`'s "2026 Award Recipients" `<table>` contains 3 nested `<p>` tags) is excluded from the top-level block walk so its text is captured exactly once, via the ancestor block's `get_text()`, not duplicated.

Live recon also confirmed real per-program dollar amounts and deadline dates are present in this page family's prose (e.g. `$5,000` grant caps, `September 30, 2026` deadlines) — the real per-program data DECISIONS #32 found missing one directory level up. No amount/date field is parsed out of that text into a typed value; `published_date` stays `None` for every item (nullable per `.claude/rules/data.md`, not a sentinel), same discipline as DECISIONS #29/#33/#34, since a page can embed several dates (deadlines, RFP rounds, past-recipient-list years) with no single one reliably "the" effective date.
Why: A closed, code-enforced URL list (DECISIONS #35/#36) plus one honestly-scoped parser is more defensible under review than a parser that silently guesses at program boundaries it cannot verify — and avoids two near-duplicate dataclasses for a distinction that turned out not to be reliably detectable from the markup itself.
Date: 2026-08-20

## #38 — stpete.org scope expanded a fourth directory level: 3 named pages linked from `for_business_owners.php`
Decision: The stpete.org source is expanded once more, to the 3 pages DECISIONS #36 found linked from `for_business_owners.php` (itself one of DECISIONS #35's 23 URLs, confirmed to be a further hub rather than content). Confirmed live on 2026-08-20 via `StpeteProgramDetailsCrawler.crawl_further_hub_page()`:
- `https://www.stpete.org/residents/grants___loans/grow_smarter.php` (Grow Smarter Job Creation and Talent Attraction Program)
- `https://www.stpete.org/business/legacy_business_program.php` (Legacy Business Program — note this one is under a `business/` path, not `residents/grants___loans/` or `residents/housing/...` like every prior entry; still `www.stpete.org`, already allow-listed per DECISIONS #31)
- `https://www.stpete.org/residents/grants___loans/tax_incentives.php` (Tax Incentives)

DECISIONS #36 also flagged an open question this entry does not resolve: whether any of the other 22 DECISIONS #35 detail pages conceal a similar further-hub link inside their prose (DECISIONS #37's parser captures dedup'd hrefs per `<h2>` block but nothing has checked those captured hrefs for further stpete.org hub pages). That check, and any URLs it surfaces, needs its own future DECISIONS entry — this entry authorizes exactly the 3 URLs above and does not pre-authorize a fifth directory level or a retroactive sweep of the existing 22.
Why: Same closed/finite/code-enforceable discipline as #11/#17/#30/#35 — Amber asked for a 5th round specifically to close out this one known hub before deciding whether stpete.org is done for this cycle.
Date: 2026-08-20

## #39 — DECISIONS #38's 3 pages confirmed same h2-sectioned template; no fifth directory level found among them
Decision: `app/crawlers/stpete_program_details.py` is extended with `FURTHER_HUB_DETAIL_URLS` (`grow_smarter.php`, `business/legacy_business_program.php`, `tax_incentives.php`) and `StpeteProgramDetailsCrawler.crawl_further_hub_detail_pages()`, reusing the existing `parse_program_detail_page()` — no new parser was written. Live recon (2026-08-20, via `BaseCrawler.fetch()` — robots.txt checked, honest User-Agent, rate-limited) confirmed all 3 pages share the same `#post .module-container` / `<h1>`/`<h2>`/`<h3>`/`<h4>` shape DECISIONS #37 documented for the 22 pages one directory level up:
- `grow_smarter.php`: 2 `<h2>`s ("Grant Overview", "Documents"), real dollar amounts present (`$3,000`, `$2,000`).
- `business/legacy_business_program.php`: 5 `<h2>`s, one (`How to Submit a Nomination`) noting the nomination period "is now closed", and one (`2026 Honorees`) whose `2026 Finalists` `<h3>` has 6 nested `<h4>` district headings ("District 1" through "District 8", 2 districts missing finalists) — the same h4-folds-into-parent-h3 behavior DECISIONS #37 established for a single `<h4>`, confirmed here to also hold for multiple consecutive `<h4>`s under one `<h3>` without modification. No dollar amounts or dates anywhere on this page.
- `tax_incentives.php`: 5 `<h2>`s, each naming a distinct incentive program (Ad Valorem Tax Exemption, Brownfield Redevelopment Bonus, Capital Investment Tax Credit, Reduced Transportation Impact Fee, Urban Job Tax Credit) rather than a generic section label — the same DECISIONS #37 "N programs under one page" ambiguous shape as `solar.php`, each with its own nested `<h3>Overview</h3>`/`<h4>Documents</h4>` pair. Real dollar amounts present (e.g. `$100,000`, `$2,500`, `$1,500` per-job credits) and 2 real effective dates in prose (`October 18, 2012`; `June 15, 2017`), consistent with DECISIONS #37's practice of leaving these in `text` verbatim rather than parsing a typed field.

None of the 3 pages is itself a further hub: no `div.v2-tiles-con` tile grid was found on any of them, and none contains an internal `stpete.org`/`www.stpete.org` link anywhere in its content (checked directly against each page's extracted `<a href>`s during recon — all links are either document files under `stpete.org`, external sites, or `library.municode.com`/`leg.state.fl.us`/`egis.stpete.org` — the last a different subdomain, same deferred-subdomain treatment as DECISIONS #35's `police.stpete.org` exclusion). No fifth-directory-level blocker to flag from this entry.

Why: The template match was confirmed live rather than assumed, per the task's own instruction and the standing DECISIONS #27/#30/#32/#35/#36 practice of not trusting a URL's directory depth to predict its shape. Reusing `parse_program_detail_page()` keeps one source of truth for this page family's parsing, same reasoning as DECISIONS #37 itself. `FURTHER_HUB_DETAIL_URLS` is kept as a separate tuple from `PROGRAM_DETAIL_URLS` (not merged) so the code continues to make visible which DECISIONS entry authorizes which URLs, rather than flattening two different scope grants into one list.
Date: 2026-08-20

## #40 — DECISIONS #36's open question closed: the 22 existing detail pages do conceal further stpete.org links; candidate list for a possible round 6
Decision: Per DECISIONS #36/#38's explicitly-left-open question, all `<h2>`-section `links` captured by `StpeteProgramDetailsCrawler.parse_program_detail_page()` across the 22 DECISIONS #35 detail-page fixtures were checked (read-only, no live fetch, no crawler built) for internal `stpete.org`/`www.stpete.org` hrefs not already named by DECISIONS #11/#30/#35/#38. 70 distinct hrefs total were captured across all 22 pages; 41 resolve to `stpete.org`/`www.stpete.org` internally. Of those 41, 31 are document files (PDF/XLSX/JPG under `stpete.org`, not further HTML pages) already excluded from consideration as "hub" candidates. The remaining 10 are genuinely new, uncovered page links:
- `https://www.stpete.org/residents/grants___loans/cra_housing-based_grants.php` (found in `for_property_owners.php`) — reads like another grant-program page, same template family as the 22.
- `https://www.stpete.org/residents/housing/housing_opportunities_for_all.php` (found in `rebates_for_affordable_residential_rehabs.php`)
- `https://www.stpete.org/residents/housing/income_limits.php` (found in 4 of the 22 pages: `affordable_housing_lot_disposition_program.php`, `housing_rehabilitation_assistance_program.php`, `purchase_assistance_program.php`, `rebates_for_affordable_residential_rehabs.php`)
- `https://www.stpete.org/residents/housing/documents.php` (found in `consolidated_plan.php`)
- `https://www.stpete.org/government/boards___committees/arts_advisory_committee.php` (found in `level_up_arts_grant.php`)
- `https://www.stpete.org/government/boards___committees/youth_development_review_committee.php` (found in `youth_development_grants.php`)
- `https://www.stpete.org/government/mayor___city_council/mayor_s_office/vision.php` (found in `social_action_funding.php`)
- `https://www.stpete.org/affordablehousing`, `https://www.stpete.org/arpa`, `https://www.stpete.org/artsnewsletter` (found in `social_action_funding.php`, `gov_youth_opportunity_grants.php`, `level_up_arts_grant.php` respectively) — extensionless vanity/redirect URLs; their actual resolved content/shape is unconfirmed, not fetched here.

None of these 10 were fetched or parsed — this entry is the investigation record, not a build. **A round 6 does look warranted** if per-program data on these pages (especially `cra_housing-based_grants.php`, which by name may be another grant program in the same underserved-scope gap DECISIONS #32 originally flagged) is wanted; it would need its own DECISIONS entry naming these exact URLs (and confirming the 3 vanity URLs' actual resolved targets/shapes live) before any crawler follows them.
Why: DECISIONS #36 flagged this exact check as unclosed when DECISIONS #38 was written, and the task explicitly asked to close it out one way or the other. Recording "10 new candidates found," not silently deciding whether to build them, keeps the same discipline as every prior entry in this chain — the finding is Amber's to act on, not an assumed authorization to keep crawling.
Date: 2026-08-20
