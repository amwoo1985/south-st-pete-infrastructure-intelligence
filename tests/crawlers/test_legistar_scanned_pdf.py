"""Gap 3: the PDF fail-loud-on-empty-text path, proven against an actual
no-text-layer PDF, not just asserted to exist in the code.

DECISIONS.md documents why this fixture is synthetic: a bounded live-corpus
search (agenda + minutes PDFs sampled across 2015, 2018, 2020, 2022, 2024,
20 documents total) found every single one was a real text-layer PDF - no
scanned/image-only Legistar PDF turned up in that sample. tests/fixtures/
legistar/agenda_scanned_no_text.pdf is a single-page PDF built by
rasterizing text onto a bitmap image and embedding only that image (no PDF
text objects at all), so pypdf's extract_text() legitimately returns "".
"""

from __future__ import annotations

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.legistar import LegistarCrawler
from tests.conftest import load_fixture_bytes, register_robots_permissive
from tests.crawlers.test_legistar import make_crawler, make_meeting


def test_extract_pdf_text_raises_on_scanned_no_text_layer_pdf():
    pdf_bytes = load_fixture_bytes("agenda_scanned_no_text.pdf")
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no extractable text"):
        crawler._extract_pdf_text(pdf_bytes, "https://pinellas.legistar.com/fake-scanned.pdf")


@responses.activate
def test_resolve_agenda_content_fails_loud_on_scanned_pdf_end_to_end():
    """Same assertion, but through the real fetch -> resolve_agenda_content
    path a live crawl run would actually take, not just the extraction
    helper in isolation."""
    pdf_url = "https://pinellas.legistar.com/View.ashx?M=A&ID=9999999&GUID=SCANNED"
    register_robots_permissive(responses)
    responses.add(
        responses.GET,
        pdf_url,
        body=load_fixture_bytes("agenda_scanned_no_text.pdf"),
        status=200,
        content_type="application/pdf",
    )
    meeting = make_meeting(accessible_agenda_html_url=None, agenda_pdf_url=pdf_url)

    crawler: LegistarCrawler = make_crawler()
    with pytest.raises(CrawlerStructureError):
        crawler.resolve_agenda_content(meeting)
