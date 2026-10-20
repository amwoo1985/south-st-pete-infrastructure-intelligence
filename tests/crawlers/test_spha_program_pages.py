"""Tests for app/crawlers/spha_program_pages.py - DECISIONS #48/#49's 8
`www.stpeteha.org` program pages.

Fixtures recorded from the real live pages via this module's own
crawler's fetch() during this session - see DECISIONS #48/#50.

Unlike every prior stpete.org host in this crawl chain, `www.stpeteha.org`
serves a real (not 404) robots.txt: `User-agent: *\\nDisallow: ` (no
restrictions declared, but a live 200 response, not an absent file) -
confirmed live 2026-08-20. `register_robots_permissive` (which registers a
404) is not reused here; each test that exercises `fetch()` registers this
host's actual robots.txt body instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.spha_program_pages import (
    AFFORDABLE_HOUSING_CLIENTS_URL,
    ANNUAL_PLANS_URL,
    FSS_PROGRAM_URL,
    HOMEOWNERSHIP_URL,
    HOUSING_URL,
    PERFORMANCE_REPORT_URL,
    PROGRAM_PAGE_URLS,
    PUBLIC_HOUSING_CLIENTS_URL,
    SECTION_8_HCV_VOUCHER_HOLDER_URL,
    SphaProgramPage,
    SphaProgramPagesCrawler,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "spha_program_pages"

_ROBOTS_BODY = "User-agent: *\nDisallow: \nSitemap: https://www.stpeteha.org/sitemap.xml\n"

_URL_TO_SLUG = {
    HOUSING_URL: "housing",
    PUBLIC_HOUSING_CLIENTS_URL: "public-housing-clients",
    AFFORDABLE_HOUSING_CLIENTS_URL: "affordable-housing-clients",
    SECTION_8_HCV_VOUCHER_HOLDER_URL: "section-8-hcv-voucher-holder",
    FSS_PROGRAM_URL: "fss-program",
    HOMEOWNERSHIP_URL: "homeownership",
    ANNUAL_PLANS_URL: "annual-plans",
    PERFORMANCE_REPORT_URL: "performance-report",
}


def load_fixture(slug: str) -> str:
    return (FIXTURES_DIR / f"{slug}.html").read_text(encoding="utf-8")


def make_crawler() -> SphaProgramPagesCrawler:
    return SphaProgramPagesCrawler(min_request_interval_seconds=0)


def register_spha_robots(responses_mock) -> None:
    responses_mock.add(
        responses_mock.GET,
        "https://www.stpeteha.org/robots.txt",
        body=_ROBOTS_BODY,
        status=200,
    )


# --- Real-fixture parsing, all 8 pages ---------------------------------------


@pytest.mark.parametrize("url", PROGRAM_PAGE_URLS)
def test_parse_program_page_real_fixture_shape(url):
    slug = _URL_TO_SLUG[url]
    html = load_fixture(slug)
    crawler = make_crawler()
    page = crawler.parse_program_page(html, url)

    assert isinstance(page, SphaProgramPage)
    assert page.page_title
    assert page.page_url == url
    assert len(page.sections) >= 1
    for section in page.sections:
        assert section.heading is None or isinstance(section.heading, str)
        # Every section carries real content - text or at least a link.
        assert section.text or section.links
        # Mandatory attribution per .claude/rules/crawler.md.
    assert page.attribution.source_url == url
    assert page.attribution.retrieval_timestamp.tzinfo is not None
    # No page attests one canonical effective date - see module docstring.
    # Nullable per .claude/rules/data.md, not a sentinel.
    assert page.attribution.published_date is None


def test_housing_page_titles_and_headings():
    html = load_fixture("housing")
    page = make_crawler().parse_program_page(html, HOUSING_URL)

    assert page.page_title == "Housing"
    headings = [s.heading for s in page.sections]
    assert headings == [None, "Housing Programs", "Programs"]
    lead = page.sections[0]
    assert "4,000 households" in lead.text


def test_public_housing_clients_faq_eligibility_content():
    html = load_fixture("public-housing-clients")
    page = make_crawler().parse_program_page(html, PUBLIC_HOUSING_CLIENTS_URL)

    faq = next(s for s in page.sections if s.heading == "FAQ")
    assert "80 percent of the Area Median Income" in faq.text
    assert "30 percent of their adjusted monthly income" in html  # sanity: real source text present


def test_affordable_housing_clients_application_fee_and_ami_thresholds():
    html = load_fixture("affordable-housing-clients")
    page = make_crawler().parse_program_page(html, AFFORDABLE_HOUSING_CLIENTS_URL)

    faq = next(s for s in page.sections if s.heading == "FAQ")
    assert "$50.00" in faq.text
    assert "140% AMI" in faq.text


def test_section_8_uses_lower_ami_threshold_than_public_housing():
    """Section 8/HCV's real income threshold (50% AMI) differs from Public
    Housing's (80% AMI, DECISIONS #48/#50) - both captured verbatim, not
    collapsed into one figure."""
    html = load_fixture("section-8-hcv-voucher-holder")
    page = make_crawler().parse_program_page(html, SECTION_8_HCV_VOUCHER_HOLDER_URL)

    lead = page.sections[0]
    assert "50 percent of the Area Median Income" in lead.text

    headings = [s.heading for s in page.sections]
    assert headings == [
        None,
        "FAQ",
        "Renting a Unit",
        "Inspections",
        "Recertification",
        "Program Forms and Information",
        "Scholarship Program",
    ]


def test_section_8_project_based_voucher_counts_captured():
    html = load_fixture("section-8-hcv-voucher-holder")
    page = make_crawler().parse_program_page(html, SECTION_8_HCV_VOUCHER_HOLDER_URL)

    lead = page.sections[0]
    assert "105 project-based vouchers" in lead.text
    assert "Bay Pointe Tower" in lead.text


def test_fss_program_eligibility_content():
    html = load_fixture("fss-program")
    page = make_crawler().parse_program_page(html, FSS_PROGRAM_URL)

    eligibility = next(s for s in page.sections if s.heading == "Eligibility")
    assert "5-Year Contract of Participation" in eligibility.text


def test_homeownership_hcv_eligibility_and_stray_h1_content_captured():
    """The stray, real mid-content <h1> found live on this page (a
    realtor's name pasted from another site's markup, see module
    docstring) is captured as ordinary text, not silently dropped."""
    html = load_fixture("homeownership")
    page = make_crawler().parse_program_page(html, HOMEOWNERSHIP_URL)

    lead = page.sections[0]
    assert "SPHA Family Self-Sufficiency (FSS) Program" in lead.text

    realtor_section = next(s for s in page.sections if s.heading and "Realtor" in s.heading)
    assert "Archer Realty Solutions" in realtor_section.text


def test_annual_plans_deadline_date_embedded_in_text():
    html = load_fixture("annual-plans")
    page = make_crawler().parse_program_page(html, ANNUAL_PLANS_URL)

    assert page.sections[0].heading == "Proposed Amendments and Revisions"
    assert "September 24, 2026" in page.sections[0].text


def test_performance_report_single_lead_section_no_headings():
    """No 36px section heading exists anywhere on this page - a legitimate
    single-section shape, not a structure-parsing failure."""
    html = load_fixture("performance-report")
    page = make_crawler().parse_program_page(html, PERFORMANCE_REPORT_URL)

    assert len(page.sections) == 1
    assert page.sections[0].heading is None
    assert "2025 Performance Report" in page.sections[0].text
    assert len(page.sections[0].links) >= 1


# --- CTA-button vs real heading disambiguation -------------------------------


def test_cta_button_styled_at_36px_is_not_treated_as_its_own_section():
    """public-housing-clients' "CLICK HERE to go the RentPayment page..."
    button is styled with the same 36px/bold as a real heading but wraps
    an <a href> - confirmed live this must not create a spurious section
    between "Paying Rent" and "Maintenance / Work Order Requests"."""
    html = load_fixture("public-housing-clients")
    page = make_crawler().parse_program_page(html, PUBLIC_HOUSING_CLIENTS_URL)

    headings = [s.heading for s in page.sections]
    assert "CLICK HERE to go the RentPayment page to pay your rent. >" not in headings

    paying_rent = next(s for s in page.sections if s.heading == "Paying Rent")
    assert any("rentpayment.com" in link for link in paying_rent.links)


def test_section_8_empty_jump_anchor_inside_heading_still_recognized():
    """section-8's real headings each wrap an empty in-page-jump <a id=...>
    with no href (e.g. `<a id="RentingaUnit"></a>Renting a Unit`) -
    confirmed live this must still count as a real heading, unlike a CTA
    <a href=...> button."""
    html = load_fixture("section-8-hcv-voucher-holder")
    page = make_crawler().parse_program_page(html, SECTION_8_HCV_VOUCHER_HOLDER_URL)

    assert any(s.heading == "Renting a Unit" for s in page.sections)


# --- Fail-loud: structure-parsing failures -----------------------------------


def test_parse_missing_title_raises():
    html = "<html><body><div id='bodyContainer'><p>content</p></div></body></html>"
    with pytest.raises(CrawlerStructureError, match="page title"):
        make_crawler().parse_program_page(html, HOUSING_URL)


def test_parse_empty_title_raises():
    html = "<html><body><h1 class='ptitles'></h1><div id='bodyContainer'><p>content</p></div></body></html>"
    with pytest.raises(CrawlerStructureError, match="empty text"):
        make_crawler().parse_program_page(html, HOUSING_URL)


def test_parse_missing_content_container_raises():
    html = "<html><body><h1 class='ptitles'>Housing</h1></body></html>"
    with pytest.raises(CrawlerStructureError, match="content container"):
        make_crawler().parse_program_page(html, HOUSING_URL)


def test_parse_empty_content_container_raises():
    html = "<html><body><h1 class='ptitles'>Housing</h1><div id='bodyContainer'></div></body></html>"
    with pytest.raises(CrawlerStructureError, match="no content blocks"):
        make_crawler().parse_program_page(html, HOUSING_URL)


def test_parse_two_headings_in_one_block_raises():
    html = """
    <html><body>
    <h1 class="ptitles">Housing</h1>
    <div id="bodyContainer">
        <p>
            <span style="font-size: 36px;"><strong>First Heading</strong></span>
            <span style="font-size: 36px;"><strong>Second Heading</strong></span>
        </p>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="distinct section headings"):
        make_crawler().parse_program_page(html, HOUSING_URL)


def test_parse_heading_span_with_empty_text_raises():
    html = """
    <html><body>
    <h1 class="ptitles">Housing</h1>
    <div id="bodyContainer">
        <p><span style="font-size: 36px;"><strong></strong></span></p>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="empty text"):
        make_crawler().parse_program_page(html, HOUSING_URL)


# --- crawl() end-to-end against mocked HTTP ---------------------------------


@responses.activate
def test_crawl_fetches_and_parses_all_8_pages():
    register_spha_robots(responses)
    for url in PROGRAM_PAGE_URLS:
        responses.add(responses.GET, url, body=load_fixture(_URL_TO_SLUG[url]), status=200)

    crawler = make_crawler()
    pages = crawler.crawl()

    assert set(pages.keys()) == set(PROGRAM_PAGE_URLS)
    for url, page in pages.items():
        assert page.page_url == url
        assert page.attribution.source_url == url
