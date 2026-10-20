"""Bounded, one-shot Granicus transcription-worker entrypoint — the
container command for the new `south-st-pete-granicus-worker` scheduled
Fargate task family, invoked every 15 minutes by an EventBridge Schedule.
Recorded as DECISIONS #139 (deploy-review/crawler-review pre-commit
finding, this round: don't let a forward reference to a not-yet-written
entry read as already-decided fact — the #139 entry itself is written and
landed in this same round, after this code is real-redeployed and
live-verified, same precedent as #137's Dockerfile.migrate fix).

Replaces the always-on `south-st-pete-worker` ECS service (DECISIONS
#138's cpu=2048/memory=9216 sizing, ~$88.31/mo running 24/7 whether or
not there's anything pending) with a scheduled one-off task at the SAME
sizing — load-bearing per #138's real measured OOM peak, not shrunk here
just because invocations are now short-lived — that only actually costs
money while it's doing work, not while idle. Amber-approved architecture
this round (task brief, task a35c382555630eef5's recommendation).

Sequence, in order, matters:

  1. `recover_stale_claims(conn)` FIRST, before claiming any new work.
     This closes a real, separate gap flagged this round:
     `app/granicus/worker.py` built and tested `recover_stale_claims`/
     `STALE_CLAIM_THRESHOLD` (tests/granicus/test_worker.py) but no
     production code path ever actually called it — not the old
     `run_worker.run_forever()` loop, not anywhere else. It would have
     auto-recovered the two `granicus_transcription_jobs` rows orphaned
     by today's OOM crashes (DECISIONS #137/#138) within
     `STALE_CLAIM_THRESHOLD` (4 hours), with no manual DB fix required.
     Wiring it in here, at the start of every scheduled invocation,
     means a worker that dies mid-job (OOM, task killed by Fargate,
     etc.) self-heals on the very next 15-minute tick instead of a row
     sitting stuck in `status='claimed'` until a human notices.

  2. `process_pending_jobs(conn, openai_client, crawler,
     max_jobs=MAX_JOBS_PER_INVOCATION)` — bounded, not unbounded. See
     `MAX_JOBS_PER_INVOCATION` below for the worked reasoning.

  0. (Before any of the above.) Try to take a Postgres session-scoped
     advisory lock (`pg_try_advisory_lock`) unique to this job. See
     `_SINGLE_FLIGHT_LOCK_KEY` below — closes a real gap crawler-review
     and deploy-review both independently flagged this round (MEDIUM):
     `MAX_JOBS_PER_INVOCATION` bounds work *within* one invocation, but
     nothing previously stopped two overlapping scheduled ticks (e.g. one
     invocation still mid-job when the next 15-minute tick fires) from
     BOTH actively claiming and processing jobs at once. `FOR UPDATE SKIP
     LOCKED` made that DB-safe, but not cost-safe (two concurrent
     `cpu=2048/memory=9216` Fargate tasks billed instead of one) and not
     etiquette-safe (`.claude/rules/crawler.md`'s 2.0s/host rate-limit
     spacing is enforced per-`BaseCrawler`-instance, so two concurrent
     invocations each independently pacing at 2s means the CDN sees a
     combined rate of however many invocations happen to overlap, not one
     coordinated cadence). A single-flight lock makes an overlapping
     invocation a fast, cheap no-op (log + exit 0) instead of a second
     real worker.

  3. Exit. A clean run (including "queue was empty" — matches
     `process_one_job`'s own "returns None on nothing pending, that's
     not an error" contract) exits 0. Any *per-job* failure (a bad
     audio file, an oversized file, a transient network blip that
     exhausted its retries) is already caught and recorded to
     `granicus_transcription_jobs.failure_reason` by `process_one_job`
     itself (`.claude/rules/crawler.md`: fail loud with a stored reason,
     never silently empty) — this script does not re-raise for that
     case, and still exits 0, because the *invocation* ran fine even
     though one job's outcome was 'failed'. The one exception:
     `process_pending_jobs` re-raises a systemic OpenAI
     auth/permission failure (`openai.AuthenticationError` /
     `PermissionDeniedError`) on purpose, per its own documented
     contract (a broken credential isn't specific to one job) — that
     propagates out of `main()` uncaught, giving this invocation a
     non-zero exit code and a visible CloudWatch stack trace, which is
     exactly the right signal for a scheduled task (unlike
     `run_worker.run_forever()`'s old "keep polling forever, log
     loudly" choice, appropriate there because there was no "next
     invocation" separate from "the same never-exiting process" — here
     there is, so letting a genuinely broken-credential invocation end
     visibly non-zero rather than silently succeeding-with-nothing-done
     is the more honest signal for whoever/whatever is watching
     invocation health).


## `MAX_JOBS_PER_INVOCATION = 2`: worked reasoning

Not unbounded, and not the old service loop's `JOBS_PER_CYCLE = 1`
either — deliberately in between, matching this project's own stated
convention (`.claude/rules/crawler.md`: "validate a small batch") and
the orchestrator's brief for this round ("small backlog burst
tolerance... not unbounded").

- **Why not unbounded.** A single job's realistic worst-case wall time is
  ~78 minutes (DECISIONS #138's "A'" derivation, worker.py's module
  docstring) and its pessimistic cross-check is ~178-179 minutes. An
  unbounded `max_jobs` on a 15-minute cadence risks one invocation
  running long enough that several later scheduled ticks fire while it's
  still going. `claim_next_job`'s `FOR UPDATE SKIP LOCKED` makes that
  race SAFE (a later tick's own `south-st-pete-granicus-worker` task
  just finds every already-claimed row skipped, per
  `app/granicus/worker.py`'s own docstring) — but "safe" is not "free":
  each overlapping tick is still a separate billed Fargate task at this
  family's `cpu=2048/memory=9216` sizing. Bounding `max_jobs` bounds how
  long any one invocation can plausibly run, which bounds how much
  overlap (and therefore how much of that "safe but not free" cost) a
  burst of newly-registered meetings can actually cause.
- **Why not 1.** `JOBS_PER_CYCLE = 1` was the right choice for
  `run_worker.run_forever()` because that loop needed to re-check a
  shutdown flag between jobs (see its own module docstring) — a concern
  that doesn't exist here; this script has no shutdown flag; it runs
  once and exits. Capping at 1 here would mean a burst of 2+ newly
  registered meetings only ever gets processed one-per-tick, i.e. no
  faster than the old always-on loop's own throughput, which defeats
  the "small backlog burst tolerance" this round's brief explicitly
  asked for — including the concrete, real, present case of the two
  jobs orphaned by today's OOM crashes both being pending at once as
  soon as Amber resets them.
- **Why not more than 2.** At the realistic 78-minute figure, 2 jobs
  back-to-back is ~156 minutes (~2.6 hours) — already ~10 scheduled
  ticks' worth of potential overlap before this invocation would
  finish. A 3rd or 4th job pushes that further while buying comparably
  little: any backlog deeper than 2 still drains within a small number
  of 15-minute ticks either way (the next invocation picks up right
  where this one left off), so the marginal benefit of raising this past
  2 is small while the marginal cost (longer plausible overlap window)
  keeps growing linearly. 2 clears the real, current case (both
  orphaned jobs in one invocation) without chasing an unbounded backlog
  in a single run.

If a future backlog genuinely needs faster draining than one small
capped batch every 15 minutes reliably provides, that's a signal to
revisit the schedule interval or this constant explicitly (with its own
DECISIONS.md entry, per `.claude/rules/deploy.md`) — not a reason to
quietly raise this number without re-deriving the reasoning above.


## `_SINGLE_FLIGHT_LOCK_KEY`: worked reasoning

A fixed, arbitrary `bigint` (`84271139` — no meaning beyond "unique to
this job within this database," picked once and never reused for another
lock in this codebase) passed to `pg_try_advisory_lock`. Session-scoped:
held for the lifetime of this script's one DB connection and released
automatically when that connection closes (`finally: conn.close()`
below) — no separate `pg_advisory_unlock` call needed, and no risk of a
leaked lock outliving a crashed process the way a row-level flag would.

If a second invocation's `pg_try_advisory_lock` call returns `false`
(another invocation already holds it), this script logs and returns
immediately — WITHOUT calling `recover_stale_claims` or
`process_pending_jobs` at all. This is deliberately a full early return,
not a partial "skip claiming but still recover stale claims" — a stale
claim reclaimed by an overlapping second invocation while the first is
still actively working an unrelated job is themselves both safe under
`FOR UPDATE SKIP LOCKED`, but there is no benefit to running the recovery
scan twice per real 15-minute window, and keeping the early return simple
(one lock, one gate, nothing runs without it) is easier to reason about
than a partial-bypass.

This does NOT replace `MAX_JOBS_PER_INVOCATION`'s own bound — it bounds a
different axis. `MAX_JOBS_PER_INVOCATION` bounds how much work one
invocation that DOES hold the lock will do; the lock bounds how many
invocations are ever doing real work AT ALL at the same time (always at
most 1, by construction). Together: at most one billed
`cpu=2048/memory=9216` task actively working at any moment, and that one
task's own run is itself capped at ~2 jobs before it exits and releases
the lock for the next tick.


## Residual gap, disclosed rather than silently accepted (deploy-review/
## crawler-review LOW, this round)

The old `run_worker.run_forever()` loop tracked consecutive errored
polling cycles and emitted an aggregated heartbeat WARNING after
sustained failure (see its own module docstring). This one-shot model has
no equivalent: each invocation is independent, and per this script's own
contract, only a systemic OpenAI credential failure produces a non-zero
exit — every other per-job failure (a CDN block, a source-site structure
change, repeated transient-network exhaustion) still exits 0 every 15
minutes, visible only as scattered per-row `failure_reason`s and
per-invocation CloudWatch ERROR lines, never as a single "last N
invocations all degraded" signal. Acceptable for this round (no such
status/health view exists anywhere in this codebase yet for ANY
crawler/pipeline, per CLAUDE.md's own open "needs at least basic
visibility" note) but named here explicitly rather than left implicit,
for whoever eventually builds that view.


## What this script deliberately does NOT do

Does not touch `granicus_transcription_jobs` rows directly (no
hand-written SQL here at all) — every DB write goes through the already
tested, already reviewed `app/granicus/worker.py` functions
(`recover_stale_claims`, `process_pending_jobs` -> `process_one_job`).
Does not run an unattended full 12-month backfill — `max_jobs` stays
small on every invocation, matching `.claude/rules/crawler.md`. Does not
change `STALE_CLAIM_THRESHOLD` or any other worker.py tunable — this is
a new *invocation shape* around already-decided logic, not a change to
that logic itself.

Usage (inside the container, as the task definition's command — see
`infra/granicus-worker-taskdef.template.json`; matches this project's
existing `scripts/` invocation convention, e.g.
`scripts/embed_granicus_transcripts.py` — a plain script path, not a
`-m package.module` form, since `scripts/` is not a package (no
`__init__.py`) anywhere else in this codebase either):

    python scripts/run_granicus_worker_once.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import openai

from app.crawlers.base import BaseCrawler
from app.db.connection import get_connection
from app.granicus.schema import apply_granicus_schema
from app.granicus.worker import process_pending_jobs, recover_stale_claims

logger = logging.getLogger("granicus.run_once")

logging.basicConfig(level=logging.INFO)

# See module docstring's "MAX_JOBS_PER_INVOCATION" section for the worked
# reasoning behind this value.
MAX_JOBS_PER_INVOCATION = 2

# See module docstring's "_SINGLE_FLIGHT_LOCK_KEY" section for the worked
# reasoning. Arbitrary, unique-within-this-codebase bigint — not derived
# from anything, just picked once.
_SINGLE_FLIGHT_LOCK_KEY = 84271139


def main() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_SINGLE_FLIGHT_LOCK_KEY,))
            (acquired,) = cur.fetchone()
        if not acquired:
            logger.info(
                "another invocation already holds the single-flight lock "
                "(key=%d) — exiting without claiming any work this tick",
                _SINGLE_FLIGHT_LOCK_KEY,
            )
            return

        # Idempotent CREATE TABLE/INDEX IF NOT EXISTS, same convention as
        # app/granicus/run_worker.py's main() and
        # scripts/embed_granicus_transcripts.py — safe regardless of
        # invocation order relative to other services.
        apply_granicus_schema(conn)

        # recover_stale_claims already logs its own WARNING with the full
        # reclaimed list when it finds anything (app/granicus/worker.py) —
        # not re-logged here, just a short empty-case note for symmetry.
        reclaimed = recover_stale_claims(conn)
        if not reclaimed:
            logger.info("no stale claimed jobs to recover")

        openai_client = openai.OpenAI()
        crawler = BaseCrawler(source_name="granicus-worker")

        results = process_pending_jobs(
            conn, openai_client, crawler, max_jobs=MAX_JOBS_PER_INVOCATION
        )
        if results:
            logger.info(
                "processed %d job(s) this invocation: %s",
                len(results),
                [(r.mp3_url, r.status, r.failure_reason) for r in results],
            )
        else:
            logger.info("queue empty, nothing to process this invocation")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
