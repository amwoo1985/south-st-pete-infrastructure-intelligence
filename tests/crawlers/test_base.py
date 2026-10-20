"""Tests for app/crawlers/base.py — the shared fetch/robots/rate-limit/
scope-enforcement infrastructure every crawler builds on.

Covers, per the Day 2 self-review gap list:
- rate-limiter timing (mocked clock, no real sleep)
- robots.txt handling: 404-as-permissive, disallow, and unverifiable-fails-
  loud
- honest User-Agent on outgoing requests
- closed source-list enforcement (ScopeViolationError)
"""

from __future__ import annotations

import responses

from app.crawlers.base import (
    ALLOWED_SOURCE_HOSTS,
    BaseCrawler,
    RateLimiter,
    RobotsError,
    ScopeViolationError,
)
from tests.conftest import register_robots_permissive

# --- RateLimiter: mocked clock, no real time.sleep in a unit test --------


def test_rate_limiter_sleeps_for_remaining_interval(monkeypatch):
    limiter = RateLimiter(min_interval_seconds=2.0)

    fake_now = [100.0]
    sleeps: list[float] = []

    monkeypatch.setattr("app.crawlers.base.time.monotonic", lambda: fake_now[0])
    monkeypatch.setattr("app.crawlers.base.time.sleep", lambda s: sleeps.append(s))

    limiter.wait("example.com")  # first call: no prior request, no sleep
    assert sleeps == []

    fake_now[0] = 100.5  # only 0.5s elapsed, need to wait 1.5s more
    limiter.wait("example.com")
    assert sleeps == [1.5]


def test_rate_limiter_does_not_sleep_once_interval_elapsed(monkeypatch):
    limiter = RateLimiter(min_interval_seconds=2.0)

    fake_now = [100.0]
    sleeps: list[float] = []

    monkeypatch.setattr("app.crawlers.base.time.monotonic", lambda: fake_now[0])
    monkeypatch.setattr("app.crawlers.base.time.sleep", lambda s: sleeps.append(s))

    limiter.wait("example.com")
    fake_now[0] = 103.0  # 3s elapsed, already past the 2.0s minimum
    limiter.wait("example.com")
    assert sleeps == []


def test_rate_limiter_is_per_host(monkeypatch):
    limiter = RateLimiter(min_interval_seconds=2.0)
    fake_now = [100.0]
    sleeps: list[float] = []
    monkeypatch.setattr("app.crawlers.base.time.monotonic", lambda: fake_now[0])
    monkeypatch.setattr("app.crawlers.base.time.sleep", lambda s: sleeps.append(s))

    limiter.wait("host-a.com")
    limiter.wait("host-b.com")  # different host, same instant: no sleep
    assert sleeps == []


# --- Scope enforcement -----------------------------------------------------


def test_fetch_raises_scope_violation_for_disallowed_host():
    crawler = BaseCrawler(source_name="test")
    assert "evil.example.com" not in ALLOWED_SOURCE_HOSTS
    try:
        crawler.fetch("https://evil.example.com/whatever")
        assert False, "expected ScopeViolationError"
    except ScopeViolationError:
        pass


# --- robots.txt handling ----------------------------------------------------


@responses.activate
def test_robots_txt_404_is_treated_as_no_restrictions():
    register_robots_permissive(responses, host="pinellas.legistar.com")
    responses.add(
        responses.GET,
        "https://pinellas.legistar.com/Calendar.aspx",
        body="<html>ok</html>",
        status=200,
    )

    crawler = BaseCrawler(source_name="test", min_request_interval_seconds=0)
    resp = crawler.fetch("https://pinellas.legistar.com/Calendar.aspx")
    assert resp.status_code == 200


@responses.activate
def test_robots_txt_disallow_raises_robots_error():
    responses.add(
        responses.GET,
        "https://pinellas.legistar.com/robots.txt",
        body="User-agent: *\nDisallow: /Calendar.aspx\n",
        status=200,
    )

    crawler = BaseCrawler(source_name="test", min_request_interval_seconds=0)
    try:
        crawler.fetch("https://pinellas.legistar.com/Calendar.aspx")
        assert False, "expected RobotsError"
    except RobotsError:
        pass


@responses.activate
def test_robots_txt_fetch_failure_fails_loud_not_permissive():
    """A non-404 failure fetching robots.txt must raise RobotsError, never
    be silently treated as "allowed" — this is the distinction DECISIONS
    #16 calls out explicitly."""
    responses.add(
        responses.GET,
        "https://pinellas.legistar.com/robots.txt",
        status=503,
    )

    crawler = BaseCrawler(source_name="test", min_request_interval_seconds=0)
    try:
        crawler.fetch("https://pinellas.legistar.com/Calendar.aspx")
        assert False, "expected RobotsError"
    except RobotsError:
        pass


# --- Narrow per-host 403-on-robots.txt exception (DECISIONS #89/#91) -------


@responses.activate
def test_robots_txt_403_treated_as_permissive_for_granicus_cdn_host_only():
    """archive-video.granicus.com is the one host where a 403 on
    robots.txt itself is treated as "no restrictions declared" (DECISIONS
    #89/#91) — every other host's 403 must still raise RobotsError
    (covered by test_robots_txt_403_is_not_permissive_for_other_hosts
    below), so this test alone would not catch an accidental broadening of
    the exception to all hosts."""
    responses.add(
        responses.GET,
        "https://archive-video.granicus.com/robots.txt",
        status=403,
    )
    responses.add(
        responses.GET,
        "https://archive-video.granicus.com/some-meeting.mp3",
        body=b"fake-audio-bytes",
        status=200,
    )

    crawler = BaseCrawler(source_name="test", min_request_interval_seconds=0)
    resp = crawler.fetch("https://archive-video.granicus.com/some-meeting.mp3")
    assert resp.status_code == 200


@responses.activate
def test_robots_txt_403_is_not_permissive_for_other_hosts():
    """The 403-as-permissive exception is scoped to
    ROBOTS_403_TREATED_AS_PERMISSIVE_HOSTS only — a 403 on any other
    host's robots.txt (pinellas.legistar.com here) must still raise
    RobotsError exactly as before DECISIONS #91, proving this isn't a
    general loosening of the 404-only rule."""
    responses.add(
        responses.GET,
        "https://pinellas.legistar.com/robots.txt",
        status=403,
    )

    crawler = BaseCrawler(source_name="test", min_request_interval_seconds=0)
    try:
        crawler.fetch("https://pinellas.legistar.com/Calendar.aspx")
        assert False, "expected RobotsError"
    except RobotsError:
        pass


# --- Honest User-Agent -------------------------------------------------------


@responses.activate
def test_fetch_sends_configured_user_agent():
    register_robots_permissive(responses, host="pinellas.legistar.com")
    responses.add(
        responses.GET,
        "https://pinellas.legistar.com/Calendar.aspx",
        body="<html>ok</html>",
        status=200,
    )

    crawler = BaseCrawler(
        source_name="test",
        user_agent="TestAgent/1.0 (+contact: test@example.com)",
        min_request_interval_seconds=0,
    )
    crawler.fetch("https://pinellas.legistar.com/Calendar.aspx")

    # Two requests happened: robots.txt, then the page. Both must carry the
    # real, non-browser-spoofed User-Agent - never silently default to
    # requests' own "python-requests/x.y" string.
    assert len(responses.calls) == 2
    for call in responses.calls:
        assert call.request.headers["User-Agent"] == "TestAgent/1.0 (+contact: test@example.com)"
