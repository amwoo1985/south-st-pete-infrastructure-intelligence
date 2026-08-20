"""Tests for app/crawlers/pinellas_hcd.py - DECISIONS #48/#52's 11
`pinellas.gov` pages (10 program pages + 1 department overview page).

Fixtures recorded from the real live pages via a generic fetch during this
session's recon, then confirmed parseable against this module's own
parser - see DECISIONS #48/#52/#53.

`pinellas.gov` serves a real (not 404) Yoast-plugin robots.txt - same
"real 200, no restrictions declared" shape as `www.stpeteha.org`
(DECISIONS #50) - `register_pinellas_robots` registers its actual body.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.pinellas_hcd import (
    AYUDA_PAGO_INICIAL_URL,
    COMMUNITY_DEVELOPMENT_NSP_URL,
    DEPARTMENT_OVERVIEW_URL,
    FLORIDA_SHIP_URL,
    HOME_INVESTMENT_PARTNERSHIPS_URL,
    HOME_REPAIR_LOAN_PROGRAM_URL,
    HURRICANE_HOME_REPAIR_PROGRAM_URL,
    INDEPENDENT_LIVING_PROGRAM_URL,
    LEALMAN_COMMERCIAL_IMPROVEMENT_GRANT_URL,
    LEALMAN_RESIDENTIAL_IMPROVEMENT_GRANT_URL,
    PRESTAMO_REPARACION_VIVIENDAS_ES_URL,
    PROGRAM_PAGE_URLS,
    PinellasDepartmentPage,
    PinellasHcdCrawler,
    PinellasProgramPage,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "pinellas_hcd"

_ROBOTS_BODY = """# START YOAST BLOCK
User-agent: *
Disallow:

Sitemap: https://pinellas.gov/sitemap_index.xml
# END YOAST BLOCK

Disallow: /wp-admin/*
Allow: /wp-content/uploads/*
"""

_URL_TO_SLUG = {
    AYUDA_PAGO_INICIAL_URL: "ayuda-pago-inicial",
    COMMUNITY_DEVELOPMENT_NSP_URL: "community-development-neighborhood-stabilization-program",
    FLORIDA_SHIP_URL: "florida-state-housing-initiatives-partnership-program",
    HOME_INVESTMENT_PARTNERSHIPS_URL: "home-investment-partnerships-program",
    HOME_REPAIR_LOAN_PROGRAM_URL: "home-repair-loan-program",
    INDEPENDENT_LIVING_PROGRAM_URL: "independent-living-program",
    LEALMAN_COMMERCIAL_IMPROVEMENT_GRANT_URL: "lealman-commercial-improvement-grant-program",
    LEALMAN_RESIDENTIAL_IMPROVEMENT_GRANT_URL: "lealman-residential-improvement-grant-program",
    HURRICANE_HOME_REPAIR_PROGRAM_URL: "pinellas-county-hurricane-home-repair-program",
    PRESTAMO_REPARACION_VIVIENDAS_ES_URL: (
        "programas-de-prestamo-para-la-reparacion-de-viviendas-y-de-vida-independiente-revision-in-progress"
    ),
}


def load_fixture(slug: str) -> str:
    return (FIXTURES_DIR / f"{slug}.html").read_text(encoding="utf-8")


def make_crawler() -> PinellasHcdCrawler:
    return PinellasHcdCrawler(min_request_interval_seconds=0)


def register_pinellas_robots(responses_mock) -> None:
    responses_mock.add(
        responses_mock.GET,
        "https://pinellas.gov/robots.txt",
        body=_ROBOTS_BODY,
        status=200,
    )


# --- Real-fixture parsing, all 10 program pages ------------------------------


@pytest.mark.parametrize("url", PROGRAM_PAGE_URLS)
def test_parse_program_page_real_fixture_shape(url):
    slug = _URL_TO_SLUG[url]
    html = load_fixture(slug)
    page = make_crawler().parse_program_page(html, url)

    assert isinstance(page, PinellasProgramPage)
    assert page.page_title
    assert page.page_url == url
    assert len(page.sections) >= 1
    for section in page.sections:
        assert section.heading is None or isinstance(section.heading, str)
        assert section.level is None or section.level in ("h2", "h3", "h4", "h5", "h6")
        # Every section carries real content - text, a link, or (confirmed
        # live on a few pages, e.g. lealman-commercial's "Eligible
        # Improvements") a bare wrapper heading immediately followed by a
        # deeper-level heading with the real content, and so a legitimate
        # empty section rather than a parsing gap.
        assert section.text or section.links or section.heading is not None
    assert page.attribution.source_url == url
    assert page.attribution.retrieval_timestamp.tzinfo is not None
    # No page attests one canonical effective date - see module docstring.
    # Nullable per .claude/rules/data.md, not a sentinel.
    assert page.attribution.published_date is None


def test_home_repair_loan_program_dollar_figures_and_closed_status():
    """DECISIONS #48's own recon flagged a $75,000 figure on a news article
    NOT in this round's authorized scope - this confirms the same figure
    is independently, directly stated on the authorized
    home-repair-loan-program page itself."""
    html = load_fixture("home-repair-loan-program")
    page = make_crawler().parse_program_page(html, HOME_REPAIR_LOAN_PROGRAM_URL)

    all_text = " ".join(s.text or "" for s in page.sections)
    assert "$75,000" in all_text
    assert "$20,000" in all_text  # Independent Living cross-reference
    assert "CLOSED" in all_text
    assert "5/4/2026" in all_text


def test_hurricane_program_dollar_figure_and_ami_table():
    html = load_fixture("pinellas-county-hurricane-home-repair-program")
    page = make_crawler().parse_program_page(html, HURRICANE_HOME_REPAIR_PROGRAM_URL)

    lead = next(s for s in page.sections if s.heading and "$30,000" in s.heading)
    assert "APPLICATIONS CLOSED" in lead.text

    quals = next(s for s in page.sections if s.heading == "Program Qualifications")
    assert "$87,600" in quals.text  # 1-person 120% AMI limit


def test_lealman_residential_grant_cap_and_match_table():
    html = load_fixture("lealman-residential-improvement-grant-program")
    page = make_crawler().parse_program_page(html, LEALMAN_RESIDENTIAL_IMPROVEMENT_GRANT_URL)

    lead = page.sections[0]
    assert "$15,000" in lead.text

    match_table = next(s for s in page.sections if s.heading == "Matching Grant Amount Based on AMI%")
    assert "50% match" in match_table.text


def test_lealman_commercial_grant_cap():
    html = load_fixture("lealman-commercial-improvement-grant-program")
    page = make_crawler().parse_program_page(html, LEALMAN_COMMERCIAL_IMPROVEMENT_GRANT_URL)

    funds = next(s for s in page.sections if s.heading == "Lealman Commercial Improvement Grant- Funds")
    assert "$100,000" in funds.text


def test_independent_living_program_grant_cap():
    html = load_fixture("independent-living-program")
    page = make_crawler().parse_program_page(html, INDEPENDENT_LIVING_PROGRAM_URL)

    lead = page.sections[0]
    assert "$10,000" in lead.text


def test_nsp_round_funding_totals():
    html = load_fixture("community-development-neighborhood-stabilization-program")
    page = make_crawler().parse_program_page(html, COMMUNITY_DEVELOPMENT_NSP_URL)

    headings = [s.heading for s in page.sections if s.heading]
    assert any("$4.6 Million" in h for h in headings)
    assert any("$8 Million" in h for h in headings)
    assert any("$17.8 Million" in h for h in headings)


def test_ayuda_pago_inicial_spanish_page_has_real_dollar_figures():
    """Spanish-language page, built the same as the English pages - not
    thin/placeholder content."""
    html = load_fixture("ayuda-pago-inicial")
    page = make_crawler().parse_program_page(html, AYUDA_PAGO_INICIAL_URL)

    assert page.page_title == "Ayuda para el pago inicial"
    all_text = " ".join(s.text or "" for s in page.sections)
    assert "$75,000" in all_text
    assert "$50,000" in all_text

    headings = [s.heading for s in page.sections if s.heading]
    assert "Requisitos de elegibilidad" in headings


def test_prestamo_reparacion_es_spanish_page_not_thin_despite_url_slug():
    """This URL's 'revision-in-progress' slug does not mean thin/placeholder
    content - confirmed live it carries the same real $75,000/$20,000
    figures as the English home-repair-loan-program page."""
    html = load_fixture(
        "programas-de-prestamo-para-la-reparacion-de-viviendas-y-de-vida-independiente-revision-in-progress"
    )
    page = make_crawler().parse_program_page(html, PRESTAMO_REPARACION_VIVIENDAS_ES_URL)

    all_text = " ".join(s.text or "" for s in page.sections)
    assert "$75,000" in all_text
    assert "$20,000" in all_text


def test_home_investment_partnerships_eligibility_percentages():
    html = load_fixture("home-investment-partnerships-program")
    page = make_crawler().parse_program_page(html, HOME_INVESTMENT_PARTNERSHIPS_URL)

    eligibility = next(s for s in page.sections if s.heading == "Eligibility")
    assert "60% of the HUD-adjusted median family income" in eligibility.text or "60%" in eligibility.text


def test_ship_program_purpose_no_current_dollar_figure():
    """SHIP's own page states program purpose/eligibility prose but ties no
    dollar figure to a currently open funding round - a real difference
    from the other 9 program pages, not a parsing gap."""
    html = load_fixture("florida-state-housing-initiatives-partnership-program")
    page = make_crawler().parse_program_page(html, FLORIDA_SHIP_URL)

    lead = page.sections[0]
    assert "very low-, low- and moderate-income" in lead.text or "moderate-income" in lead.text


def test_bare_apply_now_anchor_link_captured_not_dropped():
    """The hurricane page's 'Apply Now' button (div.wp-block-buttons > a,
    not wrapped in a <p>/<ul>/<ol>/<table>) must still have its href
    captured - see module docstring."""
    html = load_fixture("pinellas-county-hurricane-home-repair-program")
    page = make_crawler().parse_program_page(html, HURRICANE_HOME_REPAIR_PROGRAM_URL)

    all_links = [link for s in page.sections for link in s.links]
    assert any("neighborlysoftware.com" in link for link in all_links)


# --- Real-fixture parsing, department overview page --------------------------


def test_parse_department_page_real_fixture_shape():
    html = load_fixture("department")
    page = make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)

    assert isinstance(page, PinellasDepartmentPage)
    assert page.page_title == "Housing and Community Development Department"
    assert page.page_url == DEPARTMENT_OVERVIEW_URL
    assert page.description
    assert len(page.stat_cards) >= 2
    assert len(page.hub_sections) == 5
    assert page.attribution.source_url == DEPARTMENT_OVERVIEW_URL
    assert page.attribution.published_date is None


def test_department_page_quick_facts_stat_cards_real_content():
    html = load_fixture("department")
    page = make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)

    population = next(c for c in page.stat_cards if c.heading == "Our Population is Growing")
    assert "30,000 permanent residents" in population.text

    housing = next(c for c in page.stat_cards if c.heading == "Affordable Housing Found Here")
    assert "$23.4 million" in housing.text


def test_department_page_accomplishments_cards_link_off_pinellas_gov():
    """2 of the 3 Accomplishments cards link to hosts outside
    ALLOWED_SOURCE_HOSTS entirely (homesforpinellas.org, plan.pinellas.gov)
    - captured for transparency, never fetched."""
    html = load_fixture("department")
    page = make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)

    all_links = [link for c in page.stat_cards for link in c.links]
    assert any("homesforpinellas.org" in link for link in all_links)
    assert any("plan.pinellas.gov" in link for link in all_links)


def test_department_page_news_hub_includes_decisions_48_unauthorized_url():
    """Confirms DECISIONS #48's own note: the $75,000 news article is
    reachable from this hub page but is NOT one of the 11 authorized URLs -
    captured as a link-only title/url pair, never fetched."""
    html = load_fixture("department")
    page = make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)

    news_section = next(h for h in page.hub_sections if h.heading == "News & Stories")
    urls = [link.url for link in news_section.links]
    assert (
        "https://pinellas.gov/news/pinellas-reopens-home-repair-program-offering-up-to-75000-in-assistance/" in urls
    )


def test_department_page_programs_hub_links_are_subset_of_authorized_program_urls():
    """The department page's own 'Programs' hub-listing links to 7 of this
    round's 10 program pages - a subset, never a page outside them."""
    html = load_fixture("department")
    page = make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)

    programs_section = next(h for h in page.hub_sections if h.heading == "Programs")
    urls = {link.url for link in programs_section.links}
    assert urls.issubset(set(PROGRAM_PAGE_URLS))
    assert len(urls) > 0


def test_department_page_services_hub_links_are_all_out_of_scope():
    """Every 'Services' hub link is an application-form page not named in
    DECISIONS #48/#52 - confirms this section is a genuine blocker, not
    already-covered ground."""
    html = load_fixture("department")
    page = make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)

    services_section = next(h for h in page.hub_sections if h.heading == "Services")
    urls = {link.url for link in services_section.links}
    authorized = set(PROGRAM_PAGE_URLS) | {DEPARTMENT_OVERVIEW_URL}
    assert urls.isdisjoint(authorized)
    assert all("/services/" in u for u in urls)


# --- Fail-loud: program page structure-parsing failures ----------------------


def test_program_page_missing_content_container_raises():
    html = "<html><body><h1>Title</h1></body></html>"
    with pytest.raises(CrawlerStructureError, match="content container"):
        make_crawler().parse_program_page(html, HOME_REPAIR_LOAN_PROGRAM_URL)


def test_program_page_missing_h1_raises():
    html = '<html><body><div id="section-content"><p>content</p></div></body></html>'
    with pytest.raises(CrawlerStructureError, match="page title"):
        make_crawler().parse_program_page(html, HOME_REPAIR_LOAN_PROGRAM_URL)


def test_program_page_empty_h1_raises():
    html = '<html><body><div id="section-content"><h1></h1><p>content</p></div></body></html>'
    with pytest.raises(CrawlerStructureError, match="empty text"):
        make_crawler().parse_program_page(html, HOME_REPAIR_LOAN_PROGRAM_URL)


def test_program_page_empty_heading_raises():
    html = """
    <html><body>
    <div id="section-content">
        <h1>Title</h1>
        <h2>   </h2>
        <p>content</p>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="empty text"):
        make_crawler().parse_program_page(html, HOME_REPAIR_LOAN_PROGRAM_URL)


def test_program_page_no_content_blocks_raises():
    html = '<html><body><div id="section-content"><h1>Title</h1></div></body></html>'
    with pytest.raises(CrawlerStructureError, match="no content parsed"):
        make_crawler().parse_program_page(html, HOME_REPAIR_LOAN_PROGRAM_URL)


# --- Fail-loud: department page structure-parsing failures -------------------


def test_department_page_missing_hero_h1_raises():
    html = '<html><body><div id="section-content"></div></body></html>'
    with pytest.raises(CrawlerStructureError, match="hero heading"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_empty_hero_h1_raises():
    html = '<html><body><h1 class="title"></h1><div id="section-content"></div></body></html>'
    with pytest.raises(CrawlerStructureError, match="empty text"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_missing_content_container_raises():
    html = '<html><body><h1 class="title">Dept</h1></body></html>'
    with pytest.raises(CrawlerStructureError, match="content container"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_missing_quick_container_raises():
    html = """
    <html><body>
    <h1 class="title">Dept</h1>
    <div id="section-content"><h2>Programs</h2><div class="listings-container"><a href="/x">X</a></div></div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="quick-container"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_quick_container_no_cards_raises():
    html = """
    <html><body>
    <h1 class="title">Dept</h1>
    <div id="section-content">
        <div id="quick-container"><h2>Quick Facts</h2></div>
        <h2>Programs</h2><div class="listings-container"><a href="/x">X</a></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="stat cards"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_stat_card_missing_strong_raises():
    html = """
    <html><body>
    <h1 class="title">Dept</h1>
    <div id="section-content">
        <div id="quick-container">
            <h2>Quick Facts</h2>
            <div class="card-deck"><div class="card"><p>no heading here</p></div></div>
        </div>
        <h2>Programs</h2><div class="listings-container"><a href="/x">X</a></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="no <strong> heading"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_no_hub_headings_raises():
    html = """
    <html><body>
    <h1 class="title">Dept</h1>
    <div id="section-content">
        <div id="quick-container">
            <h2>Quick Facts</h2>
            <div class="card-deck"><div class="card"><strong>Fact</strong><p>text</p></div></div>
        </div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="hub-section"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_hub_section_no_links_raises():
    html = """
    <html><body>
    <h1 class="title">Dept</h1>
    <div id="section-content">
        <div id="quick-container">
            <h2>Quick Facts</h2>
            <div class="card-deck"><div class="card"><strong>Fact</strong><p>text</p></div></div>
        </div>
        <div class="d-flex"><h2>Programs</h2></div>
        <div class="listings-container"><p>no links here</p></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="no real"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


def test_department_page_hub_section_no_following_container_raises():
    html = """
    <html><body>
    <h1 class="title">Dept</h1>
    <div id="section-content">
        <div id="quick-container">
            <h2>Quick Facts</h2>
            <div class="card-deck"><div class="card"><strong>Fact</strong><p>text</p></div></div>
        </div>
        <div class="d-flex"><h2>Programs</h2></div>
    </div>
    </body></html>
    """
    with pytest.raises(CrawlerStructureError, match="no following content container"):
        make_crawler().parse_department_page(html, DEPARTMENT_OVERVIEW_URL)


# --- crawl() end-to-end against mocked HTTP ----------------------------------


@responses.activate
def test_crawl_program_pages_fetches_and_parses_all_10():
    register_pinellas_robots(responses)
    for url in PROGRAM_PAGE_URLS:
        responses.add(responses.GET, url, body=load_fixture(_URL_TO_SLUG[url]), status=200)

    crawler = make_crawler()
    pages = crawler.crawl_program_pages()

    assert set(pages.keys()) == set(PROGRAM_PAGE_URLS)
    for url, page in pages.items():
        assert page.page_url == url
        assert page.attribution.source_url == url


@responses.activate
def test_crawl_department_page_fetches_and_parses():
    register_pinellas_robots(responses)
    responses.add(responses.GET, DEPARTMENT_OVERVIEW_URL, body=load_fixture("department"), status=200)

    crawler = make_crawler()
    pages = crawler.crawl_department_page()

    assert set(pages.keys()) == {DEPARTMENT_OVERVIEW_URL}
    page = pages[DEPARTMENT_OVERVIEW_URL]
    assert page.page_url == DEPARTMENT_OVERVIEW_URL
    assert page.attribution.source_url == DEPARTMENT_OVERVIEW_URL
