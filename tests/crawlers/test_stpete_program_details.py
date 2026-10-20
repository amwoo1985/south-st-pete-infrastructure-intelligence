"""Tests for app/crawlers/stpete_program_details.py - the 22 h2-sectioned
per-program detail pages plus the 1 further-hub page (for_business_owners.php)
found among the URLs DECISIONS #35 names (count corrected in DECISIONS #36).

Fixtures recorded from the real live pages via this module's own crawler's
fetch() during this session - see DECISIONS #35-37.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_program_details import (
    AFFORDABLE_HOUSING_LOT_DISPOSITION_URL,
    ARTS_GRANTS_PROGRAM_URL,
    COMMUNITY_FOOD_GRANT_PROGRAM_URL,
    COMMUNITY_IMPACT_SUMMER_ENHANCEMENT_GRANT_URL,
    CONSOLIDATED_PLAN_URL,
    CRA_HOUSING_BASED_GRANTS_URL,
    DECISIONS_41_URLS,
    EDUCATION_YOUTH_OPPORTUNITY_GRANTS_URL,
    FOR_BUSINESS_OWNERS_URL,
    FOR_DEVELOPERS_URL,
    FOR_PROPERTY_OWNERS_URL,
    FURTHER_HUB_DETAIL_URLS,
    GOV_YOUTH_OPPORTUNITY_GRANTS_URL,
    GROW_SMARTER_URL,
    HOUSING_REHABILITATION_ASSISTANCE_URL,
    INDIVIDUAL_ARTIST_GRANT_URL,
    LEGACY_BUSINESS_PROGRAM_URL,
    LEVEL_UP_ARTS_GRANT_URL,
    MAYORS_NEIGHBORHOOD_MINI_GRANT_URL,
    MLK_COMMUNITIES_IN_ACTION_URL,
    MULTI_FAMILY_RENTAL_LOAN_PROGRAM_URL,
    NEIGHBORHOOD_PARTNERSHIP_MATCHING_GRANTS_URL,
    PROGRAM_DETAIL_URLS,
    PURCHASE_ASSISTANCE_PROGRAM_URL,
    REBATES_FOR_AFFORDABLE_RESIDENTIAL_REHABS_URL,
    SOCIAL_ACTION_FUNDING_URL,
    SOLAR_URL,
    STORMWATER_UTILITY_FEE_CREDITS_URL,
    TAX_INCENTIVES_URL,
    YOUTH_DEVELOPMENT_GRANTS_URL,
    StpeteProgramDetailsCrawler,
)
from tests.conftest import register_robots_permissive

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_program_details"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_crawler() -> StpeteProgramDetailsCrawler:
    return StpeteProgramDetailsCrawler(min_request_interval_seconds=0)


# --- Real-fixture parsing: shape and attribution, per detail page ----------

DETAIL_PAGE_CASES = [
    ("for_developers.html", FOR_DEVELOPERS_URL, 3),
    ("for_property_owners.html", FOR_PROPERTY_OWNERS_URL, 13),
    ("arts_grants_program.html", ARTS_GRANTS_PROGRAM_URL, 3),
    ("community_food_grant_program.html", COMMUNITY_FOOD_GRANT_PROGRAM_URL, 3),
    ("individual_artist_grant.html", INDIVIDUAL_ARTIST_GRANT_URL, 3),
    ("level_up_arts_grant.html", LEVEL_UP_ARTS_GRANT_URL, 1),
    ("mayors_neighborhood_mini-grant_program.html", MAYORS_NEIGHBORHOOD_MINI_GRANT_URL, 1),
    ("mlk_communities_in_action_mini-grant_program.html", MLK_COMMUNITIES_IN_ACTION_URL, 3),
    ("neighborhood_partnership_matching_grants.html", NEIGHBORHOOD_PARTNERSHIP_MATCHING_GRANTS_URL, 1),
    ("social_action_funding.html", SOCIAL_ACTION_FUNDING_URL, 3),
    ("stormwater_utility_fee_credits.html", STORMWATER_UTILITY_FEE_CREDITS_URL, 2),
    ("affordable_housing_lot_disposition_program.html", AFFORDABLE_HOUSING_LOT_DISPOSITION_URL, 1),
    ("consolidated_plan.html", CONSOLIDATED_PLAN_URL, 2),
    ("housing_rehabilitation_assistance_program.html", HOUSING_REHABILITATION_ASSISTANCE_URL, 3),
    ("multi-family_rental_loan_program.html", MULTI_FAMILY_RENTAL_LOAN_PROGRAM_URL, 3),
    ("purchase_assistance_program.html", PURCHASE_ASSISTANCE_PROGRAM_URL, 3),
    ("rebates_for_affordable_residential_rehabs.html", REBATES_FOR_AFFORDABLE_RESIDENTIAL_REHABS_URL, 2),
    ("solar.html", SOLAR_URL, 2),
    ("community_impact_summer_enhancement_grant.html", COMMUNITY_IMPACT_SUMMER_ENHANCEMENT_GRANT_URL, 1),
    ("education_youth_opportunity_grants.html", EDUCATION_YOUTH_OPPORTUNITY_GRANTS_URL, 1),
    ("youth_development_grants.html", YOUTH_DEVELOPMENT_GRANTS_URL, 1),
    ("gov_youth_opportunity_grants.html", GOV_YOUTH_OPPORTUNITY_GRANTS_URL, 3),
]


def test_detail_page_cases_cover_all_22_program_detail_urls():
    assert {url for _, url, _ in DETAIL_PAGE_CASES} == set(PROGRAM_DETAIL_URLS)
    assert len(PROGRAM_DETAIL_URLS) == 22


@pytest.mark.parametrize("fixture_name,url,expected_section_count", DETAIL_PAGE_CASES)
def test_parse_program_detail_page_real_fixture(fixture_name, url, expected_section_count):
    html = load_fixture(fixture_name)
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, url)

    assert page.page_title
    assert page.page_url == url
    assert len(page.sections) == expected_section_count
    for section in page.sections:
        assert section.heading
        for sub in section.subsections:
            assert sub.heading
        for link in section.links:
            assert link.startswith("http") or link.startswith("tel:") or link.startswith("mailto:")

    # Mandatory attribution per .claude/rules/crawler.md.
    assert page.attribution.source_url == url
    assert page.attribution.retrieval_timestamp.tzinfo is not None
    # No page attests one canonical effective date (deadlines/RFP rounds
    # vary per section) - nullable per .claude/rules/data.md, not a
    # sentinel. See DECISIONS #37.
    assert page.attribution.published_date is None


def test_arts_grants_program_real_amounts_and_document_links():
    html = load_fixture("arts_grants_program.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, ARTS_GRANTS_PROGRAM_URL)

    purpose = next(s for s in page.sections if s.heading == "Purpose")
    assert purpose.text is not None and "arts" in purpose.text.lower()

    documents = next(s for s in page.sections if s.heading == "Documents")
    assert any(link.endswith(".pdf?t=202603271230140") for link in documents.links)

    application_info = next(s for s in page.sections if s.heading == "Application Information")
    assert any(link.startswith("tel:") for link in application_info.links)
    assert any(link.startswith("mailto:") for link in application_info.links)


def test_community_food_grant_program_real_deadline_in_text():
    html = load_fixture("community_food_grant_program.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, COMMUNITY_FOOD_GRANT_PROGRAM_URL)

    funding_priorities = next(s for s in page.sections if s.heading == "Funding Priorities")
    project_eligibility = next(sub for sub in funding_priorities.subsections if sub.heading == "Project Eligibility")
    assert project_eligibility.text is not None
    assert "2026" in project_eligibility.text


def test_mlk_award_recipients_table_not_duplicated():
    """The '2026 Award Recipients' <table> has 3 nested <p> tags in its
    cells (confirmed live) - regression check that _is_top_level_block()
    excludes them so their text isn't counted twice. See DECISIONS #37."""
    html = load_fixture("mlk_communities_in_action_mini-grant_program.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, MLK_COMMUNITIES_IN_ACTION_URL)

    recipients = next(s for s in page.sections if "Recipients" in s.heading)
    assert recipients.text is not None
    # A phrase from one recipient's description should appear exactly
    # once, not duplicated by double-counting the table's nested <p>s.
    assert recipients.text.count("STEM Mentors in Motion") == 1


def test_housing_rehab_h4_folds_into_parent_h3():
    """housing_rehabilitation_assistance_program.php nests a <h4> ('Loan
    Features') under a <h3> ('Home Repair Loans') - confirmed live, folded
    into the h3 subsection's own text rather than promoted to its own
    boundary. See module docstring / DECISIONS #37."""
    html = load_fixture("housing_rehabilitation_assistance_program.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, HOUSING_REHABILITATION_ASSISTANCE_URL)

    overview = next(s for s in page.sections if s.heading == "Program Overview")
    sub_headings = [sub.heading for sub in overview.subsections]
    assert "Home Repair Loans" in sub_headings
    home_repair = next(sub for sub in overview.subsections if sub.heading == "Home Repair Loans")
    assert home_repair.text is not None
    assert "Loan Features:" in home_repair.text


def test_solar_two_named_sub_programs_preserved_verbatim():
    """solar.php's 2 <h2>s each name a distinct program (Solar Co-Ops,
    Switch Together Program), not a generic section label - the
    DECISIONS #37 ambiguous case. Headings are preserved as-is, not
    reclassified."""
    html = load_fixture("solar.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, SOLAR_URL)

    headings = [s.heading for s in page.sections]
    assert headings == ["Solar Co-Ops", "Switch Together Program"]


# --- DECISIONS #38: the 3 further-hub-linked pages, same template ----------

FURTHER_HUB_DETAIL_CASES = [
    ("grow_smarter.html", GROW_SMARTER_URL, 2),
    ("legacy_business_program.html", LEGACY_BUSINESS_PROGRAM_URL, 5),
    ("tax_incentives.html", TAX_INCENTIVES_URL, 5),
]


def test_further_hub_detail_cases_cover_all_3_decisions_38_urls():
    assert {url for _, url, _ in FURTHER_HUB_DETAIL_CASES} == set(FURTHER_HUB_DETAIL_URLS)
    assert len(FURTHER_HUB_DETAIL_URLS) == 3


@pytest.mark.parametrize("fixture_name,url,expected_section_count", FURTHER_HUB_DETAIL_CASES)
def test_parse_further_hub_detail_page_real_fixture(fixture_name, url, expected_section_count):
    html = load_fixture(fixture_name)
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, url)

    assert page.page_title
    assert page.page_url == url
    assert len(page.sections) == expected_section_count
    for section in page.sections:
        assert section.heading
        for sub in section.subsections:
            assert sub.heading
        for link in section.links:
            assert link.startswith("http") or link.startswith("tel:") or link.startswith("mailto:")

    assert page.attribution.source_url == url
    assert page.attribution.retrieval_timestamp.tzinfo is not None
    assert page.attribution.published_date is None


def test_grow_smarter_real_amounts_and_document_links():
    html = load_fixture("grow_smarter.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, GROW_SMARTER_URL)

    overview = next(s for s in page.sections if s.heading == "Grant Overview")
    assert overview.text is not None and "$3,000" in overview.text

    documents = next(s for s in page.sections if s.heading == "Documents")
    assert any(link.endswith(".pdf?t=202603171126100") for link in documents.links)


def test_tax_incentives_5_distinct_incentive_programs_preserved_verbatim():
    """tax_incentives.php's 5 <h2>s each name a distinct incentive program
    (not a generic section label), each with its own nested <h3>'Overview'/
    <h4>'Documents' pair - same DECISIONS #37 ambiguous-<h2> shape as
    solar.php, confirmed live. Headings preserved as-is."""
    html = load_fixture("tax_incentives.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, TAX_INCENTIVES_URL)

    headings = [s.heading for s in page.sections]
    assert headings == [
        "Ad Valorem Tax Exemption",
        "Brownfield Redevelopment Bonus",
        "Capital Investment Tax Credit (CITC)",
        "Reduced Transportation Impact Fee",
        "Urban Job Tax Credit",
    ]
    citc = next(s for s in page.sections if s.heading == "Capital Investment Tax Credit (CITC)")
    overview = next(sub for sub in citc.subsections if sub.heading == "Overview")
    assert overview.text is not None and "corporate income tax" in overview.text
    # Every section's own "Documents" <h4> folds into its "Overview" <h3>'s
    # text rather than becoming its own boundary - same fold behavior as
    # DECISIONS #37's housing_rehabilitation_assistance_program.php case.
    for section in page.sections:
        assert [sub.heading for sub in section.subsections] == ["Overview"]


def test_legacy_business_program_district_h4s_fold_into_2026_finalists_h3():
    """legacy_business_program.php nests 6 <h4> district headings under the
    <h3> '2026 Finalists' (itself under the <h2> '2026 Honorees') - folded
    into that subsection's text, same fold behavior as DECISIONS #37,
    confirmed live to repeat across multiple <h4>s rather than just one."""
    html = load_fixture("legacy_business_program.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, LEGACY_BUSINESS_PROGRAM_URL)

    honorees = next(s for s in page.sections if s.heading == "2026 Honorees")
    finalists = next(sub for sub in honorees.subsections if sub.heading == "2026 Finalists")
    assert finalists.text is not None
    for district in ("District 1:", "District 2:", "District 4:", "District 6:", "District 7:", "District 8:"):
        assert district in finalists.text


# --- crawl_further_hub_detail_pages() end-to-end against mocked HTTP -------


@responses.activate
def test_crawl_further_hub_detail_pages_fetches_and_parses_all_3():
    register_robots_permissive(responses, host="www.stpete.org")
    fixture_by_url = dict(zip((url for _, url, _ in FURTHER_HUB_DETAIL_CASES), (f for f, _, _ in FURTHER_HUB_DETAIL_CASES)))
    for url in FURTHER_HUB_DETAIL_URLS:
        responses.add(responses.GET, url, body=load_fixture(fixture_by_url[url]), status=200)

    crawler = make_crawler()
    results = crawler.crawl_further_hub_detail_pages()

    assert set(results.keys()) == set(FURTHER_HUB_DETAIL_URLS)
    for url, page in results.items():
        assert page.page_url == url
        assert page.sections


# --- Fail-loud: structure-parsing failures ----------------------------------


def test_parse_missing_content_container_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="content container"):
        crawler.parse_program_detail_page(html, ARTS_GRANTS_PROGRAM_URL)


def test_parse_content_container_missing_h1_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container"><h2>Overview</h2></div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <h1>"):
        crawler.parse_program_detail_page(html, ARTS_GRANTS_PROGRAM_URL)


def test_parse_no_h2_sections_raises():
    html = """
    <html><body>
    <div id="post"><div class="module-container">
        <h1>Some Grant Program</h1>
        <p>Intro text with no h2 section headings at all.</p>
    </div></div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no <h2> section headings"):
        crawler.parse_program_detail_page(html, ARTS_GRANTS_PROGRAM_URL)


# --- crawl_detail_pages() end-to-end against mocked HTTP -------------------


@responses.activate
def test_crawl_detail_pages_fetches_and_parses_all_22():
    register_robots_permissive(responses, host="www.stpete.org")
    fixture_by_url = dict(zip((url for _, url, _ in DETAIL_PAGE_CASES), (f for f, _, _ in DETAIL_PAGE_CASES)))
    for url in PROGRAM_DETAIL_URLS:
        responses.add(responses.GET, url, body=load_fixture(fixture_by_url[url]), status=200)

    crawler = make_crawler()
    results = crawler.crawl_detail_pages()

    assert set(results.keys()) == set(PROGRAM_DETAIL_URLS)
    for url, page in results.items():
        assert page.page_url == url
        assert page.sections


# --- for_business_owners.php: confirmed further hub, not followed ----------


def test_for_business_owners_parses_as_hub_tiles_not_detail_page():
    """Confirmed live (2026-08-20) to carry its own div.v2-tiles-con tile
    grid, not h2-sectioned detail content - see DECISIONS #36. Reuses the
    same tile parser as the DECISIONS #32 hub pages; its 3 tile links are
    never fetched (fourth directory level, not authorized)."""
    html = load_fixture("for_business_owners.html")
    crawler = make_crawler()
    tiles = crawler._tiles_parser.parse_tiles_page(html, FOR_BUSINESS_OWNERS_URL)

    assert len(tiles) == 3
    names = {t.category_name for t in tiles}
    assert names == {
        "Grow Smarter Job Creation and Talent Attraction Program",
        "Legacy Business Program",
        "Tax Incentives",
    }
    for t in tiles:
        assert t.attribution.source_url == FOR_BUSINESS_OWNERS_URL


@responses.activate
def test_crawl_further_hub_page_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(responses.GET, FOR_BUSINESS_OWNERS_URL, body=load_fixture("for_business_owners.html"), status=200)

    crawler = make_crawler()
    tiles = crawler.crawl_further_hub_page()
    assert len(tiles) == 3


# --- DECISIONS #41: cra_housing-based_grants.php, same template ------------


def test_decisions_41_urls_is_exactly_the_1_named_url():
    assert DECISIONS_41_URLS == (CRA_HOUSING_BASED_GRANTS_URL,)
    assert CRA_HOUSING_BASED_GRANTS_URL == "https://www.stpete.org/residents/grants___loans/cra_housing-based_grants.php"


def test_parse_cra_housing_based_grants_real_fixture():
    """Fixture recorded live (2026-08-20) via this module's own crawler's
    fetch() - see DECISIONS #41. Confirmed a single <h2> ('Overview')
    containing 5 <h3> subsections, one per CRA-specific housing
    sub-program - not a further hub page."""
    html = load_fixture("cra_housing-based_grants.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, CRA_HOUSING_BASED_GRANTS_URL)

    assert page.page_title == "South St. Pete Housing and Neighborhoods"
    assert page.page_url == CRA_HOUSING_BASED_GRANTS_URL
    assert len(page.sections) == 1

    overview = page.sections[0]
    assert overview.heading == "Overview"
    sub_headings = [sub.heading for sub in overview.subsections]
    assert sub_headings == [
        "Rebates for Affordable Residential Rehabs",
        "Housing Down Payment Assistance Program",
        "Housing Rehabilitation Assistance Program",
        "Affordable Single-Family Facade Improvement Grant Program",
        "Affordable Housing Redevelopment Loan Program",
    ]
    for sub in overview.subsections:
        assert sub.text

    # Mandatory attribution per .claude/rules/crawler.md.
    assert page.attribution.source_url == CRA_HOUSING_BASED_GRANTS_URL
    assert page.attribution.retrieval_timestamp.tzinfo is not None
    assert page.attribution.published_date is None


def test_cra_housing_based_grants_real_ami_threshold_in_text():
    """Real per-program detail embedded in prose (not a thin stub) - a
    120% Area Median Income threshold for the Rapid Roof Replacement
    sub-program, confirmed live."""
    html = load_fixture("cra_housing-based_grants.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, CRA_HOUSING_BASED_GRANTS_URL)

    facade = next(
        sub
        for sub in page.sections[0].subsections
        if sub.heading == "Affordable Single-Family Facade Improvement Grant Program"
    )
    assert facade.text is not None
    assert "120% Area Median Income" in facade.text


def test_cra_housing_based_grants_links_are_already_in_scope_or_declined():
    """No new stpete.org page link should appear here - every internal
    stpete.org HTML page link on this page is either already in
    PROGRAM_DETAIL_URLS or the already-declined DECISIONS #32 housing.php
    hub. Regression guard: if this ever fails, a real new candidate page
    has appeared and needs its own DECISIONS entry, not silent crawling."""
    html = load_fixture("cra_housing-based_grants.html")
    crawler = make_crawler()
    page = crawler.parse_program_detail_page(html, CRA_HOUSING_BASED_GRANTS_URL)

    already_in_scope = set(PROGRAM_DETAIL_URLS) | {
        "https://www.stpete.org/residents/grants___loans/housing.php",
    }
    for section in page.sections:
        for link in section.links:
            if link.startswith("https://www.stpete.org/") or link.startswith("https://stpete.org/"):
                if link.endswith((".pdf", ".xlsx", ".jpg")) or "?t=" in link:
                    continue  # document links, not further HTML pages
                assert link in already_in_scope, f"unexpected new stpete.org page link: {link}"


@responses.activate
def test_crawl_decisions_41_page_fetches_and_parses():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(
        responses.GET,
        CRA_HOUSING_BASED_GRANTS_URL,
        body=load_fixture("cra_housing-based_grants.html"),
        status=200,
    )

    crawler = make_crawler()
    results = crawler.crawl_decisions_41_page()

    assert set(results.keys()) == {CRA_HOUSING_BASED_GRANTS_URL}
    page = results[CRA_HOUSING_BASED_GRANTS_URL]
    assert page.page_url == CRA_HOUSING_BASED_GRANTS_URL
    assert page.sections
