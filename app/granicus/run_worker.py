"""Long-running polling-loop entrypoint for the Granicus transcription
worker — Phase J (Docker containerization). Runnable forever as:

    python -m app.granicus.run_worker

`app/granicus/worker.py`'s `process_pending_jobs()` is explicitly a thin,
bounded *batch* wrapper (see its own docstring: "NOT a default-on runner")
meant for validating a small batch from a script or test, not a service.
This module is the missing piece: a real never-exits-on-its-own service
loop around it, for the `worker` container `deploy-infra` is wiring up in
docker-compose.yml (this file does not touch that compose file or the
Dockerfile — another specialist's territory this round).


## Poll interval: worked reasoning (not a round-number guess)

`POLL_INTERVAL_SECONDS = 30.0`.

There are currently zero real registered rows in the live
`granicus_transcription_jobs` table, so in practice this container polls
an empty queue continuously until Amber runs `app/granicus/register.py`
for real meetings. Two costs to weigh, same trade-off shape as DECISIONS
#15's 2.0s crawler rate-limit and this module's neighbor
`STALE_CLAIM_THRESHOLD` derivation (worker.py's own docstring):

- **Empty-queue poll cost.** Each cycle's only DB work when nothing is
  pending is `claim_next_job`'s single `SELECT ... FOR UPDATE SKIP LOCKED
  ... LIMIT 1` against a table that, even at the full 12-month backfill
  bound (DECISIONS #12) on a weekly meeting cadence, tops out around ~52
  rows/year — trivially small, sub-millisecond regardless of whether an
  index is even hit — plus a no-op `commit()`. At 30s that's 2,880
  queries/day against a single-connection, solo-operator workload: not a
  measurable load on the smallest RDS tier this project would ever run,
  let alone local Postgres.
- **Latency once real jobs exist.** Amber registers a meeting by hand,
  human-paced (worker.py's docstring: "a few meetings at a time"), and
  reasonably expects to see it start processing within her own working
  session, not sit idle for an hour. But once a job IS picked up, a
  single job's own processing time (worker.py's docstring: ~19 min
  typical worst case under the real design, up to ~4 min download + up
  to ~15 min transcribe) dwarfs any plausible poll interval — the poll
  delay only matters for the time-to-*start*, not overall throughput.

30s clears the "starts within a normal work session" bar with room to
spare (imperceptible next to a multi-minute job), while being far coarser
than any interval that would show up as measurable DB load. Rejected
alternatives:
- **Sub-10s** (e.g. every 5s): buys nothing perceptible — shaving <30s off
  a multi-minute-per-job pickup is not a real latency win — while
  multiplying idle-poll volume 6x for zero benefit. The same
  "don't hammer for no reason" etiquette `.claude/rules/crawler.md`
  applies to external sites applies here to our own DB.
- **Minutes-to-hourly** (e.g. 5 min, 1 hr): would read as "is this
  broken?" to someone who just ran the registration CLI and is watching
  for it to pick up — the exact scenario called out in the brief.


## `JOBS_PER_CYCLE = 1`: worked reasoning

Each outer-loop iteration calls `process_pending_jobs(..., max_jobs=1)` —
i.e. claims and fully processes at most *one* job per iteration, not
`process_pending_jobs`'s own default batch shape stretched out. This is
deliberate, not an oversight: `process_pending_jobs` has no visibility
into this module's shutdown flag (see below) and, given a larger
`max_jobs`, would keep claiming and starting new jobs back-to-back inside
one call — each potentially ~19 minutes — before ever returning control
to this loop to check whether a shutdown was requested. Capping at 1 job
per outer-loop iteration means the loop re-checks the shutdown flag after
*every single job* finishes, before claiming the next one — bounding how
long a graceful shutdown can be delayed by "one more job already in
flight" (which can't be helped either way — see below) rather than by
"one more job PLUS however many more the batch would have grabbed."


## Shutdown handling

SIGTERM (Docker's `docker-compose down` signal) and SIGINT (local Ctrl+C)
both set a module-level flag checked between cycles and during the
inter-cycle sleep (via `threading.Event.wait`, so a signal during the
sleep wakes it immediately instead of waiting out the full interval).
This does NOT interrupt a job already in progress mid-download or
mid-transcription — `process_one_job` (worker.py) has no cooperative
cancellation point inside `_download_ranged`/`_transcribe_audio`, and
adding one is out of scope for this file (worker.py is another round's
concern per the brief, touched here not at all). A job caught mid-flight
when the container is killed is exactly what `recover_stale_claims`
(worker.py) and `STALE_CLAIM_THRESHOLD` already exist to reclaim; this
loop's job is only to stop claiming *new* work promptly, which the
per-job checkpoint above achieves.


## Fail-loud / per-cycle error handling

A per-cycle DB connection: this loop opens a fresh connection via
`app.db.connection.get_connection()` at the start of each cycle and
closes it at the end, rather than holding one connection for the
process's entire (potentially days-long) lifetime. Matches this
codebase's existing per-request-not-pooled convention
(app/api/dependencies.py's `get_db_conn`) and avoids a long-idle
connection silently going stale (DB restart, network blip) between the
rare cycles that do real multi-minute work.

`process_pending_jobs` re-raises exactly one exception shape on purpose
(worker.py's docstring): a systemic OpenAI auth/permission failure
(`openai.AuthenticationError`/`PermissionDeniedError`), specifically so a
batch-style caller aborts instead of burning through the whole queue
against a broken credential. In a long-running service, "abort" can't
mean "exit the process" the way it does for a bounded validation script —
Docker would just restart the container (if configured to) into the same
bad credential, and the fix (Amber updating the API key) happens by
someone editing the secret and restarting the container from outside
this process regardless of whether this process is still alive to see
it. So this loop treats it the same as any other per-cycle failure: log
loudly with the actual exception detail (`.claude/rules/crawler.md`
fail-loud — this is a credential problem needing human intervention, not
something to bury), then continue polling at the normal interval rather
than crash-looping the container. No exception type in this loop is
treated as fatal to the process — there is no case identified where
exiting would fix anything a log line plus the next poll cycle wouldn't
already surface just as visibly, and killing the process risks losing
this loop's one active job-completion checkpoint for no gain.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from types import FrameType

import openai

from app.crawlers.base import BaseCrawler
from app.db.connection import get_connection
from app.granicus.schema import apply_granicus_schema
from app.granicus.worker import process_pending_jobs

logger = logging.getLogger("granicus.run_worker")

logging.basicConfig(level=logging.INFO)

# --- Tunables (see module docstring for the worked reasoning) --------------

POLL_INTERVAL_SECONDS = 30.0
JOBS_PER_CYCLE = 1

# Heartbeat so a long idle stretch (the expected common case until Amber
# registers real meetings) still leaves a visible "yes, this is alive and
# polling" trace in the logs without flooding INFO on every single empty
# 30s cycle (CLAUDE.md: "at least basic visibility... not silent
# failure", balanced against not spamming logs for a long-running
# container). Once per ~30 min of consecutive empty polls.
_HEARTBEAT_EVERY_N_EMPTY_CYCLES = 60  # 60 * 30s = 30 min

_shutdown_event = threading.Event()


def _handle_shutdown_signal(signum: int, _frame: FrameType | None) -> None:
    logger.info("received signal %s, shutting down after current cycle", signum)
    _shutdown_event.set()


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def run_forever(openai_client: openai.OpenAI, crawler: BaseCrawler) -> None:
    """The service loop itself, factored out from `main()` so a test can
    drive it with real dependencies but stop it deterministically (set
    `_shutdown_event` from another thread, or a `max_cycles` in the
    future if needed) rather than relying on wall-clock timing alone."""
    empty_cycles_since_heartbeat = 0
    errored_cycles_since_heartbeat = 0

    while not _shutdown_event.is_set():
        conn = None
        cycle_errored = False
        try:
            # get_connection() lives inside this try deliberately: it's
            # itself a real failure point (DB restart, network blip — the
            # exact scenario this per-cycle-connection design exists to
            # tolerate, per the module docstring). Left outside the try, an
            # unguarded connect failure would propagate straight out of
            # run_forever(), killing the process with no log line and no
            # auto-recovery (the `worker` compose service has no
            # `restart:` policy) — contradicting this loop's own "no
            # exception type is fatal" contract.
            conn = get_connection()
            results = process_pending_jobs(
                conn, openai_client, crawler, max_jobs=JOBS_PER_CYCLE
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            # Systemic credential failure — see module docstring's
            # "Fail-loud / per-cycle error handling". Logged loud with
            # the real exception, not swallowed; the process keeps
            # polling rather than crash-looping the container.
            logger.error(
                "systemic OpenAI credential failure this cycle (job reverted "
                "to pending — see worker.py's _revert_to_pending): %s",
                exc,
                exc_info=True,
            )
            results = []
            cycle_errored = True
        except Exception:  # noqa: BLE001 - fail loud, keep the loop alive
            # Anything else (a transient DB blip/connect failure, an
            # unexpected error process_pending_jobs itself didn't already
            # classify and contain) — same treatment: log with full detail
            # for diagnosis, don't let one bad cycle kill the worker.
            logger.exception("unexpected error during poll cycle; continuing")
            results = []
            cycle_errored = True
        finally:
            if conn is not None:
                conn.close()

        if results:
            logger.info(
                "processed %d job(s) this cycle: %s",
                len(results),
                [(r.mp3_url, r.status, r.failure_reason) for r in results],
            )
            empty_cycles_since_heartbeat = 0
            errored_cycles_since_heartbeat = 0
        elif cycle_errored:
            empty_cycles_since_heartbeat = 0
            errored_cycles_since_heartbeat += 1
            if errored_cycles_since_heartbeat >= _HEARTBEAT_EVERY_N_EMPTY_CYCLES:
                logger.warning(
                    "heartbeat: still polling, but %d consecutive cycles have "
                    "errored — see per-cycle ERROR logs above for detail",
                    errored_cycles_since_heartbeat,
                )
                errored_cycles_since_heartbeat = 0
        else:
            errored_cycles_since_heartbeat = 0
            empty_cycles_since_heartbeat += 1
            if empty_cycles_since_heartbeat >= _HEARTBEAT_EVERY_N_EMPTY_CYCLES:
                logger.info(
                    "heartbeat: still polling, queue empty for %d consecutive cycles",
                    empty_cycles_since_heartbeat,
                )
                empty_cycles_since_heartbeat = 0
            else:
                logger.debug("poll cycle complete, queue empty")

        # Interruptible sleep: a signal during the wait wakes this
        # immediately instead of blocking out the full interval.
        _shutdown_event.wait(timeout=POLL_INTERVAL_SECONDS)

    logger.info("shutdown complete")


def main() -> None:
    _install_signal_handlers()

    openai_client = openai.OpenAI()
    crawler = BaseCrawler(source_name="granicus-worker")

    # Idempotent CREATE TABLE/INDEX IF NOT EXISTS, same convention as
    # app/api/main.py's lifespan — safe regardless of whether the `api`
    # or `worker` container happens to start first.
    conn = get_connection()
    try:
        apply_granicus_schema(conn)
    finally:
        conn.close()

    logger.info(
        "granicus worker starting: poll interval=%.0fs, jobs/cycle=%d",
        POLL_INTERVAL_SECONDS,
        JOBS_PER_CYCLE,
    )
    run_forever(openai_client, crawler)


if __name__ == "__main__":
    main()
