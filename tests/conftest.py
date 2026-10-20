"""Shared pytest fixtures for the crawler test suite.

Test strategy (see DECISIONS.md): every test exercises the real crawler
code (app/crawlers/base.py, app/crawlers/legistar.py) against either a
recorded HTML/PDF fixture under tests/fixtures/legistar/ (captured from
the live site once, during this session — see the fixture-recording note
in DECISIONS.md) or a small hand-built synthetic HTML snippet for
negative/structure-failure cases that don't need a real page. No test in
this suite makes a live network call — the `responses` library intercepts
`requests` traffic so a broken/changed live site can never make the test
suite flaky or slow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "legistar"


def load_fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


def register_robots_permissive(responses_mock, host: str = "pinellas.legistar.com") -> None:
    """Registers a 404 robots.txt response for `host` — the same "no
    restrictions declared" case DECISIONS #16 documents as confirmed live
    for Legistar. Call this in any test that exercises BaseCrawler.fetch()
    (directly or via a crawl()/resolve_*() call), since RobotsChecker
    always fetches robots.txt before the first real request to a host."""
    responses_mock.add(
        responses_mock.GET,
        f"https://{host}/robots.txt",
        status=404,
    )
