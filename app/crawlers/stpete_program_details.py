"""City of St. Petersburg grants/loans, the third-directory-level per-program
pages named in DECISIONS #35 (linked from the 4 DECISIONS #32 hub pages:
business.php, community.php, housing.php, youth.php).

Scope is exactly the URLs DECISIONS #35 names - do not add another one
without a new DECISIONS.md entry, and do not follow any link found on these
pages to a fourth directory level. DECISIONS #35 is explicit that naming
these URLs does not pre-authorize that.

DECISIONS #35's own "Named, in full" bullet list actually enumerates 9
community.php-linked URLs, one more than its own "(8 of 9 - see exclusion
below)" parenthetical count. The bullet list is treated as authoritative
here (it is arithmetically consistent with the already-recorded
community.html fixture's 10 tiles minus the 1 excluded Police Forfeiture
Grants Program link = 9) - the "8 of 9" phrase is a count typo in the
entry, not a narrower grant. This makes the real total 23 URLs, not the
22 the entry's title states; both counts are flagged in DECISIONS #36 for
Amber to reconcile. See DECISIONS #36.

Live recon (2026-08-20) found these 23 pages are **not** one shared
template - two distinct shapes:

- **A further hub page** (``for_business_owners.php`` only): the same
  ``div.v2-tiles-con``/``div.v2-tile`` tile grid as the 4 DECISIONS #32
  hub pages, linking to 3 further pages (``grow_smarter.php`` under
  ``residents/grants___loans/``, ``legacy_business_program.php`` under a
  *different* path prefix ``business/``, and ``tax_incentives.php``).
  DECISIONS #35 does not authorize a fourth directory level - these 3
  links are captured as link-only tiles (reusing
  ``StpeteGrantsCrawler.parse_tiles_page()``, per DECISIONS #32's
  precedent) but never fetched. See DECISIONS #36.

- **H2-sectioned detail pages** (the other 22 pages): every one of these
  pages has an ``<h1>`` page title inside the same ``#post
  .module-container`` region DECISIONS #33 confirmed for
  ``for_south_stpete.php``, followed by one or more ``<h2>`` blocks, each
  optionally containing nested ``<h3>`` (and rarely ``<h4>``, folded into
  its parent ``<h3>``'s text) subsections. Live recon found real
  per-program dollar amounts and deadline dates embedded in this
  page family's prose (e.g. "$5,000" grant caps, "September 30, 2026"
  deadlines) - the real per-program data DECISIONS #32 found missing from
  the hub pages one level up.

  Recon also found this ``<h2>`` level does **not** consistently mean the
  same thing across pages: on most pages each ``<h2>`` is a generic
  section label for one program (``Overview``, ``Eligibility``, ``How To
  Apply``, ``Documents``, ...), but on a few (``business/for_developers.php``,
  ``business/for_property_owners.php``, ``solar.php``,
  ``stormwater_utility_fee_credits.php``) each ``<h2>`` names a distinct
  program/credit/RFP-round in its own right. There is no reliable
  structural signal (heading text pattern, ``<h1>`` wording) that
  distinguishes "one program with N sections" from "N programs under one
  page" without guessing - both ``business/for_developers.php``'s and
  ``solar.php``'s ``<h1>``s read like ordinary program names, not umbrella
  category titles, despite each covering multiple named programs. Rather
  than force that guess into two different dataclasses (and get it wrong
  for an ambiguous page), ``StpeteProgramDetailPage`` treats every
  ``<h2>`` as one ``StpeteProgramSection`` in document order and preserves
  its heading text verbatim - callers can tell "General Eligibility" from
  "HUBZone Empowerment Contracting Program" themselves. See DECISIONS #36.

No dollar-amount or deadline field is parsed out of section text into a
typed value; per the same discipline as DECISIONS #29/#33/#34,
``published_date`` stays ``None`` (nullable per .claude/rules/data.md, not
a sentinel) rather than guessing which of a section's several embedded
dates is "the" effective date. Raw amounts/dates remain in ``text``,
verbatim.

DECISIONS #38 expands scope a fourth directory level to the 3 pages
``for_business_owners.php`` links to (``grow_smarter.php``,
``business/legacy_business_program.php``, ``tax_incentives.php``). Live
recon (2026-08-20) confirmed all 3 share this same ``#post
.module-container`` / ``<h1>``/``<h2>``/``<h3>``/``<h4>`` template - none
is itself a further hub page (no ``div.v2-tiles-con`` tile grid on any of
the 3, and no internal ``stpete.org``/``www.stpete.org`` link found in any
of their content). They're parsed with the same
``parse_program_detail_page()``, no separate parser needed. See
DECISIONS #39.

DECISIONS #41 expands scope by exactly 1 more page,
``cra_housing-based_grants.php`` (found linked from
``for_property_owners.php`` per DECISIONS #40's link sweep). Live recon
(2026-08-20) confirmed it shares the same ``#post .module-container`` /
``<h1>``/``<h2>``/``<h3>`` template: a single ``<h2>`` ("Overview")
containing 5 ``<h3>`` subsections, one per CRA-specific housing
sub-program. Not a further hub - its content links are almost entirely
back to pages already in ``PROGRAM_DETAIL_URLS``
(``rebates_for_affordable_residential_rehabs.php``,
``purchase_assistance_program.php``,
``housing_rehabilitation_assistance_program.php``) plus one link to the
already-excluded DECISIONS #32 ``housing.php`` hub; no new stpete.org page
link was found. Parsed with the same ``parse_program_detail_page()``, no
separate parser needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler
from app.crawlers.stpete_grant_categories import SOUTH_STPETE_CONTENT_SELECTOR
from app.crawlers.stpete_grants import StpeteGrantCategory, StpeteGrantsCrawler

# --- The 23 URLs DECISIONS #35 names, exactly -------------------------------
# (22 per the entry's title; the community.php group's own bullet list
# enumerates 9 rather than the entry's "8 of 9" phrase - see module
# docstring / DECISIONS #36.)

FOR_BUSINESS_OWNERS_URL = "https://www.stpete.org/residents/grants___loans/for_business_owners.php"
FOR_DEVELOPERS_URL = "https://www.stpete.org/residents/grants___loans/for_developers.php"
FOR_PROPERTY_OWNERS_URL = "https://www.stpete.org/residents/grants___loans/for_property_owners.php"

ARTS_GRANTS_PROGRAM_URL = "https://www.stpete.org/residents/grants___loans/arts_grants_program.php"
COMMUNITY_FOOD_GRANT_PROGRAM_URL = "https://www.stpete.org/residents/grants___loans/community_food_grant_program.php"
INDIVIDUAL_ARTIST_GRANT_URL = "https://www.stpete.org/residents/grants___loans/individual_artist_grant.php"
LEVEL_UP_ARTS_GRANT_URL = "https://www.stpete.org/residents/grants___loans/level_up_arts_grant.php"
MAYORS_NEIGHBORHOOD_MINI_GRANT_URL = (
    "https://www.stpete.org/residents/grants___loans/mayors_neighborhood_mini-grant_program.php"
)
MLK_COMMUNITIES_IN_ACTION_URL = (
    "https://www.stpete.org/residents/grants___loans/mlk_communities_in_action_mini-grant_program.php"
)
NEIGHBORHOOD_PARTNERSHIP_MATCHING_GRANTS_URL = (
    "https://www.stpete.org/residents/grants___loans/neighborhood_partnership_matching_grants.php"
)
SOCIAL_ACTION_FUNDING_URL = "https://www.stpete.org/residents/grants___loans/social_action_funding.php"
STORMWATER_UTILITY_FEE_CREDITS_URL = "https://www.stpete.org/residents/grants___loans/stormwater_utility_fee_credits.php"

AFFORDABLE_HOUSING_LOT_DISPOSITION_URL = (
    "https://www.stpete.org/residents/housing/developers/affordable_housing_lot_disposition_program.php"
)
CONSOLIDATED_PLAN_URL = "https://www.stpete.org/residents/housing/developers/consolidated_plan.php"
PURCHASE_ASSISTANCE_PROGRAM_URL = "https://www.stpete.org/residents/grants___loans/purchase_assistance_program.php"
HOUSING_REHABILITATION_ASSISTANCE_URL = (
    "https://www.stpete.org/residents/housing/homeowners/housing_rehabilitation_assistance_program.php"
)
MULTI_FAMILY_RENTAL_LOAN_PROGRAM_URL = (
    "https://www.stpete.org/residents/grants___loans/multi-family_rental_loan_program.php"
)
REBATES_FOR_AFFORDABLE_RESIDENTIAL_REHABS_URL = (
    "https://www.stpete.org/residents/grants___loans/rebates_for_affordable_residential_rehabs.php"
)
SOLAR_URL = "https://www.stpete.org/residents/sustainability/solar.php"

COMMUNITY_IMPACT_SUMMER_ENHANCEMENT_GRANT_URL = (
    "https://www.stpete.org/residents/grants___loans/community_impact_summer_enhancement_grant.php"
)
EDUCATION_YOUTH_OPPORTUNITY_GRANTS_URL = (
    "https://www.stpete.org/residents/grants___loans/education_youth_opportunity_grants.php"
)
YOUTH_DEVELOPMENT_GRANTS_URL = "https://www.stpete.org/residents/grants___loans/youth_development_grants.php"
GOV_YOUTH_OPPORTUNITY_GRANTS_URL = (
    "https://www.stpete.org/government/initiatives___programs/youth_opportunity_grants.php"
)

# The 22 h2-sectioned detail pages - everything DECISIONS #35 names except
# for_business_owners.php, confirmed live to be a further hub (see module
# docstring). Order mirrors DECISIONS #35's own per-hub-page grouping.
PROGRAM_DETAIL_URLS: tuple[str, ...] = (
    FOR_DEVELOPERS_URL,
    FOR_PROPERTY_OWNERS_URL,
    ARTS_GRANTS_PROGRAM_URL,
    COMMUNITY_FOOD_GRANT_PROGRAM_URL,
    INDIVIDUAL_ARTIST_GRANT_URL,
    LEVEL_UP_ARTS_GRANT_URL,
    MAYORS_NEIGHBORHOOD_MINI_GRANT_URL,
    MLK_COMMUNITIES_IN_ACTION_URL,
    NEIGHBORHOOD_PARTNERSHIP_MATCHING_GRANTS_URL,
    SOCIAL_ACTION_FUNDING_URL,
    STORMWATER_UTILITY_FEE_CREDITS_URL,
    AFFORDABLE_HOUSING_LOT_DISPOSITION_URL,
    CONSOLIDATED_PLAN_URL,
    PURCHASE_ASSISTANCE_PROGRAM_URL,
    HOUSING_REHABILITATION_ASSISTANCE_URL,
    MULTI_FAMILY_RENTAL_LOAN_PROGRAM_URL,
    REBATES_FOR_AFFORDABLE_RESIDENTIAL_REHABS_URL,
    SOLAR_URL,
    COMMUNITY_IMPACT_SUMMER_ENHANCEMENT_GRANT_URL,
    EDUCATION_YOUTH_OPPORTUNITY_GRANTS_URL,
    YOUTH_DEVELOPMENT_GRANTS_URL,
    GOV_YOUTH_OPPORTUNITY_GRANTS_URL,
)

# --- The 3 URLs DECISIONS #38 names, linked from for_business_owners.php ---

GROW_SMARTER_URL = "https://www.stpete.org/residents/grants___loans/grow_smarter.php"
LEGACY_BUSINESS_PROGRAM_URL = "https://www.stpete.org/business/legacy_business_program.php"
TAX_INCENTIVES_URL = "https://www.stpete.org/residents/grants___loans/tax_incentives.php"

# Confirmed live (2026-08-20) to share PROGRAM_DETAIL_URLS' h2-sectioned
# template - see module docstring / DECISIONS #38/#39. Kept as its own
# tuple, not merged into PROGRAM_DETAIL_URLS, because it's a distinct
# DECISIONS grant (a further directory level, only reachable via the
# for_business_owners.php hub) - collapsing the two would blur which
# DECISIONS entry authorizes which URL.
FURTHER_HUB_DETAIL_URLS: tuple[str, ...] = (
    GROW_SMARTER_URL,
    LEGACY_BUSINESS_PROGRAM_URL,
    TAX_INCENTIVES_URL,
)

# --- The 1 URL DECISIONS #41 names, exactly ---------------------------------

CRA_HOUSING_BASED_GRANTS_URL = "https://www.stpete.org/residents/grants___loans/cra_housing-based_grants.php"

# Confirmed live (2026-08-20) to share PROGRAM_DETAIL_URLS' h2-sectioned
# template - see module docstring / DECISIONS #41. Kept as its own tuple,
# not merged into PROGRAM_DETAIL_URLS, for the same reason
# FURTHER_HUB_DETAIL_URLS is kept separate: collapsing the two would blur
# which DECISIONS entry authorizes which URL.
DECISIONS_41_URLS: tuple[str, ...] = (CRA_HOUSING_BASED_GRANTS_URL,)

# Block-level content tags collected within a page's content container.
# Filtered so a <p>/<ul>/<ol>/<table> nested inside another matched block
# (e.g. a <p> inside a <table> cell, confirmed live on
# mlk_communities_in_action_mini-grant_program.php's "2026 Award
# Recipients" table) isn't counted twice - once via its own match and once
# via the ancestor block's get_text(). See DECISIONS #36.
_BLOCK_TAGS = ("p", "ul", "ol", "table")
_ALL_TAGS = ("h2", "h3", "h4", *_BLOCK_TAGS)


@dataclass(frozen=True)
class StpeteProgramSubsection:
    """One <h3> (or, rarely, a nested <h4> folded into its parent <h3>'s
    text) within a StpeteProgramSection."""

    heading: str
    text: str | None


@dataclass(frozen=True)
class StpeteProgramSection:
    """One <h2> block on a DECISIONS #35 detail page. See module
    docstring - this may be a section of a single program (``Eligibility``,
    ``How To Apply``) or itself a distinct named program/credit/RFP round,
    depending on the page; the heading text itself is the only signal, not
    forced into a semantic type here."""

    heading: str
    text: str | None
    subsections: tuple[StpeteProgramSubsection, ...]
    links: tuple[str, ...]


@dataclass(frozen=True)
class StpeteProgramDetailPage:
    """One DECISIONS #35 h2-sectioned detail page."""

    page_title: str
    page_url: str
    intro: str | None
    sections: tuple[StpeteProgramSection, ...]
    attribution: Attribution


def _is_top_level_block(el: Tag, container: Tag) -> bool:
    """True unless `el` is itself nested inside another `_BLOCK_TAGS`
    element within `container` (see `_BLOCK_TAGS` comment)."""
    for ancestor in el.parents:
        if ancestor is container:
            return True
        if getattr(ancestor, "name", None) in _BLOCK_TAGS:
            return False
    return True


class StpeteProgramDetailsCrawler(BaseCrawler):
    """Crawls DECISIONS #35's per-program detail pages plus the one
    further-hub page (`for_business_owners.php`) found among them, plus the
    3 DECISIONS #38 pages that hub page links to, plus the 1 DECISIONS #41
    page - see module docstring."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_program_details", **kwargs)
        # Reused only for its parse_tiles_page() tile-grid parser (pure
        # HTML parsing, no I/O), same pattern as
        # StpeteGrantCategoryPagesCrawler - see DECISIONS #32/#36.
        self._tiles_parser = StpeteGrantsCrawler(min_request_interval_seconds=0)

    def crawl_detail_pages(self) -> dict[str, StpeteProgramDetailPage]:
        """Fetches all 22 h2-sectioned detail pages. Returns a dict keyed
        by page URL."""
        results: dict[str, StpeteProgramDetailPage] = {}
        for url in PROGRAM_DETAIL_URLS:
            resp = self.fetch(url)
            results[url] = self.parse_program_detail_page(resp.text, url)
        return results

    def crawl_further_hub_detail_pages(self) -> dict[str, StpeteProgramDetailPage]:
        """Fetches the 3 DECISIONS #38 pages linked from
        `for_business_owners.php` (grow_smarter.php,
        business/legacy_business_program.php, tax_incentives.php). Confirmed
        live (2026-08-20) to share the same h2-sectioned template as the 22
        DECISIONS #35 detail pages - reuses `parse_program_detail_page()`
        rather than a new parser. See DECISIONS #39. Returns a dict keyed by
        page URL."""
        results: dict[str, StpeteProgramDetailPage] = {}
        for url in FURTHER_HUB_DETAIL_URLS:
            resp = self.fetch(url)
            results[url] = self.parse_program_detail_page(resp.text, url)
        return results

    def crawl_decisions_41_page(self) -> dict[str, StpeteProgramDetailPage]:
        """Fetches DECISIONS #41's 1 page (cra_housing-based_grants.php).
        Confirmed live (2026-08-20) to share the same h2-sectioned template
        as the 22 DECISIONS #35 detail pages - reuses
        `parse_program_detail_page()`. See module docstring. Returns a dict
        keyed by page URL (single entry), matching the shape of the other
        `crawl_*` methods here."""
        results: dict[str, StpeteProgramDetailPage] = {}
        for url in DECISIONS_41_URLS:
            resp = self.fetch(url)
            results[url] = self.parse_program_detail_page(resp.text, url)
        return results

    def crawl_further_hub_page(self) -> list[StpeteGrantCategory]:
        """`for_business_owners.php` - confirmed live (2026-08-20) to be
        itself another hub page (a further `div.v2-tiles-con` tile grid),
        not a program detail page. DECISIONS #35 does not authorize
        following its 3 tile links (Grow Smarter Job Creation and Talent
        Attraction Program, Legacy Business Program, Tax Incentives) - hard
        stop per the same DECISIONS #30/#35 pattern. See DECISIONS #36."""
        resp = self.fetch(FOR_BUSINESS_OWNERS_URL)
        return self._tiles_parser.parse_tiles_page(resp.text, FOR_BUSINESS_OWNERS_URL)

    def parse_program_detail_page(self, html: str, page_url: str) -> StpeteProgramDetailPage:
        soup = BeautifulSoup(html, "lxml")

        base_tag = soup.find("base")
        link_base = base_tag.get("href") if base_tag is not None and base_tag.get("href") else page_url

        container = soup.select_one(SOUTH_STPETE_CONTENT_SELECTOR)
        if container is None:
            self.fail_loud(
                f"content container {SOUTH_STPETE_CONTENT_SELECTOR!r} not found on {page_url} "
                "- stpete.org's grants/loans page structure may have changed"
            )

        h1 = container.find("h1")
        if h1 is None:
            self.fail_loud(f"content container {SOUTH_STPETE_CONTENT_SELECTOR!r} has no <h1> heading on {page_url}")
        page_title = h1.get_text(strip=True)
        if not page_title:
            self.fail_loud(f"<h1> has empty text on {page_url}")

        retrieval_time = datetime.now(timezone.utc)

        sections: list[StpeteProgramSection] = []
        intro_parts: list[str] = []

        cur_heading: str | None = None
        cur_text_parts: list[str] = []
        cur_links: list[str] = []
        cur_subsections: list[StpeteProgramSubsection] = []

        sub_heading: str | None = None
        sub_text_parts: list[str] = []

        def flush_sub() -> None:
            nonlocal sub_heading, sub_text_parts
            if sub_heading is not None:
                text = " ".join(t for t in sub_text_parts if t).strip() or None
                cur_subsections.append(StpeteProgramSubsection(heading=sub_heading, text=text))
            sub_heading = None
            sub_text_parts = []

        def flush_section() -> None:
            nonlocal cur_heading, cur_text_parts, cur_links, cur_subsections
            flush_sub()
            if cur_heading is not None:
                text = " ".join(t for t in cur_text_parts if t).strip() or None
                sections.append(
                    StpeteProgramSection(
                        heading=cur_heading,
                        text=text,
                        subsections=tuple(cur_subsections),
                        links=tuple(dict.fromkeys(cur_links)),
                    )
                )
            cur_heading = None
            cur_text_parts = []
            cur_links = []
            cur_subsections = []

        for el in container.find_all(list(_ALL_TAGS), recursive=True):
            if el.name in _BLOCK_TAGS and not _is_top_level_block(el, container):
                continue

            if el.name == "h2":
                flush_section()
                cur_heading = el.get_text(strip=True)
            elif el.name == "h3":
                if cur_heading is None:
                    # A <h3> before any <h2> (not observed live, but a
                    # future page revision could drop straight to <h3>) -
                    # promote it to a section boundary rather than
                    # silently dropping its content.
                    flush_section()
                    cur_heading = el.get_text(strip=True)
                    continue
                flush_sub()
                sub_heading = el.get_text(strip=True)
            elif el.name == "h4":
                heading_text = el.get_text(strip=True)
                if not heading_text:
                    continue
                if sub_heading is not None:
                    sub_text_parts.append(f"{heading_text}:")
                elif cur_heading is not None:
                    cur_text_parts.append(f"{heading_text}:")
            else:  # p / ul / ol / table
                text = el.get_text(" ", strip=True)
                links = [urljoin(link_base, a["href"]) for a in el.find_all("a", href=True)]
                if sub_heading is not None:
                    if text:
                        sub_text_parts.append(text)
                    cur_links.extend(links)
                elif cur_heading is not None:
                    if text:
                        cur_text_parts.append(text)
                    cur_links.extend(links)
                else:
                    # Intro prose between <h1> and the first <h2>.
                    if text:
                        intro_parts.append(text)
        flush_section()

        if not sections:
            self.fail_loud(f"no <h2> section headings found in {SOUTH_STPETE_CONTENT_SELECTOR!r} on {page_url}")

        intro = " ".join(intro_parts).strip() or None

        return StpeteProgramDetailPage(
            page_title=page_title,
            page_url=page_url,
            intro=intro,
            sections=tuple(sections),
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                # No page attests a single canonical "effective date" -
                # dates that do appear (deadlines, RFP close dates) are
                # embedded, verbatim, in section/subsection text.
                # Nullable per .claude/rules/data.md, not a sentinel.
                published_date=None,
            ),
        )
