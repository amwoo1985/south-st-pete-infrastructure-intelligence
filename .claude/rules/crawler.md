# Crawler / Scraping Rules

Binding for any code touching the Tier-1 crawlers or the Granicus transcription pipeline. Specialists (`crawler`) and reviewers (`crawler-review`) load this before acting.

## Scraping etiquette

- Respect `robots.txt` on every target site — check it, don't assume.
- Rate-limit requests. No hammering a government site; space requests out.
- Identify with a real, honest User-Agent string. Don't spoof a browser to evade blocking — if a site blocks automated access, that's a signal to stop and flag it (see DECISIONS #11's deferred-sources list for PSC/GovTrack), not a reason to circumvent it.

## Bounded scope, always

- The source list is closed per DECISIONS #11. No new source gets added without a new DECISIONS.md entry first — this file's job is to make silent scope creep structurally awkward, not just discouraged.
- The Granicus backfill window is hard-bounded to 12 months (DECISIONS #12), enforced in code — not a soft default that's easy to accidentally exceed.

## Fail loud, never silently empty

- If a target site's structure changes and a parser stops finding what it expects, raise/log an explicit error. Never let a broken parser quietly return zero results and have that look like "nothing new happened."
- Same rule for the Granicus transcription worker: a failed transcription job gets a stored failure reason, not a silently-skipped row.

## Source attribution, mandatory on every item

Every crawled or transcribed item carries, at minimum:
- Source URL
- Published/effective date (the meeting date, the grant's posted date, etc. — not just the retrieval date)
- Retrieval timestamp (when this system actually fetched it)

This is the same audit/source-attribution discipline already proven at VideoAmp/R-EX — applied here because a civic-data tool with wrong or unattributed provenance is actively worse than no tool.

## Granicus-specific (verified 2026-08-19 during Day 1 smoke test)

- Use the direct audio-only URL (`archive-video.granicus.com/stpete/<uuid>.mp3`) found on each meeting's `MediaPlayer.php` page — never the `DownloadFile.php?...` redirect, which resolves to the full multi-GB **video** file, not audio.
- The archive CDN returns a bare `403 Request blocked` to requests without a browser-like `User-Agent` header. A realistic `User-Agent` (and `Referer: https://stpete.granicus.com/`) resolves it — this is CDN bot-filtering, not an access-control or auth requirement.
- The archive CDN supports HTTP `Range` requests (confirmed: `206 Partial Content`) — use ranged requests to fetch/transcribe in chunks rather than downloading entire multi-hour files into memory at once.

## Transcription-specific

- Transcription jobs run as async background work: status column + polling worker, `FOR UPDATE SKIP LOCKED` claiming, stale-row recovery for jobs that die mid-flight. Do not attempt synchronous transcription of multi-hour audio inside an HTTP request.
- Validate a small batch (2-4 weeks of meetings) before letting the full 12-month backfill run unattended.
- Transcription API usage cost is real money at this volume — be aware of per-minute pricing before kicking off a large batch.
