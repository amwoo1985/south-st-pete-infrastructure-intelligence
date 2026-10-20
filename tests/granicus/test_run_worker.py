"""Tests for app/granicus/run_worker.py's `run_forever()` service loop —
specifically DECISIONS #142's fix: `recover_stale_claims` is now actually
called every cycle, before `process_pending_jobs` claims new work.

Pure control-flow tests, no real DB and no real OpenAI call: `get_connection`,
`process_pending_jobs`, and `recover_stale_claims` are all patched at the
`app.granicus.run_worker` module boundary (the names this module imports
them under), matching this suite's existing MagicMock-for-OpenAI convention
(tests/granicus/test_worker.py) rather than exercising the real functions
those names point at — `recover_stale_claims` itself is already covered
against the real DB in tests/granicus/test_worker.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from app.granicus import run_worker


@patch.object(run_worker._shutdown_event, "wait", return_value=None)
@patch("app.granicus.run_worker.process_pending_jobs")
@patch("app.granicus.run_worker.recover_stale_claims")
@patch("app.granicus.run_worker.get_connection")
def test_recover_stale_claims_called_before_process_pending_jobs_each_cycle(
    mock_get_connection, mock_recover, mock_process, mock_wait
):
    """One full cycle: recover_stale_claims runs first, with the cycle's
    connection, before process_pending_jobs claims anything — then the
    loop stops (simulating shutdown mid-cycle by setting the real
    `_shutdown_event` from inside a mock's side_effect, so the loop exits
    after exactly one iteration). `_shutdown_event.wait` is separately
    patched to return instantly regardless of the real 30s
    `POLL_INTERVAL_SECONDS` — this test is about call order, not timing."""
    run_worker._shutdown_event.clear()
    mock_conn = MagicMock()
    mock_get_connection.return_value = mock_conn
    mock_recover.return_value = []
    mock_process.return_value = []

    manager = MagicMock()
    manager.attach_mock(mock_recover, "recover")
    manager.attach_mock(mock_process, "process")

    def _stop_after_one_cycle(*args, **kwargs):
        run_worker._shutdown_event.set()
        return []

    mock_process.side_effect = _stop_after_one_cycle

    try:
        run_worker.run_forever(openai_client=MagicMock(), crawler=MagicMock())
    finally:
        run_worker._shutdown_event.clear()

    mock_recover.assert_called_once_with(mock_conn)
    assert mock_process.call_count == 1
    # Order matters: recovery before claiming new work (module docstring's
    # "Stale-claim recovery" section, DECISIONS #142).
    assert manager.mock_calls[0] == call.recover(mock_conn)


@patch.object(run_worker._shutdown_event, "wait", return_value=None)
@patch("app.granicus.run_worker.process_pending_jobs")
@patch("app.granicus.run_worker.recover_stale_claims")
@patch("app.granicus.run_worker.get_connection")
def test_recover_stale_claims_failure_does_not_crash_the_loop_or_block_processing(
    mock_get_connection, mock_recover, mock_process, mock_wait
):
    """A DB blip inside recover_stale_claims is caught by its own inner
    try/except (crawler-review pre-commit HIGH finding: recovery failures
    must not starve process_pending_jobs) — logged, the cycle continues to
    process_pending_jobs anyway in the SAME cycle, and the loop survives
    across multiple consecutive recovery failures rather than dying or
    silently never processing another job again."""
    run_worker._shutdown_event.clear()
    mock_conn = MagicMock()
    mock_get_connection.return_value = mock_conn

    call_count = {"n": 0}

    def _recover_side_effect(conn):
        call_count["n"] += 1
        raise RuntimeError("simulated DB blip")

    def _process_side_effect(*args, **kwargs):
        if call_count["n"] >= 2:
            run_worker._shutdown_event.set()
        return []

    mock_recover.side_effect = _recover_side_effect
    mock_process.side_effect = _process_side_effect
    fake_openai_client = MagicMock()
    fake_crawler = MagicMock()

    try:
        run_worker.run_forever(openai_client=fake_openai_client, crawler=fake_crawler)
    finally:
        run_worker._shutdown_event.clear()

    # The loop survived two consecutive recovery failures rather than
    # crashing or silently stalling — and, critically, process_pending_jobs
    # still ran in BOTH of those cycles despite recovery failing every
    # time (the exact starvation the isolated inner try/except exists to
    # prevent).
    assert mock_recover.call_count == 2
    assert mock_process.call_count == 2
    mock_process.assert_called_with(
        mock_conn, fake_openai_client, fake_crawler, max_jobs=run_worker.JOBS_PER_CYCLE
    )
