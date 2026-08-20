"""City of St. Petersburg grants/loans, the 6 category sub-pages named in
DECISIONS #30.

Scope is exactly the 6 URLs DECISIONS #30 names - do not add a 7th without
a new DECISIONS.md entry, and do not follow any link found on these 6
pages to a third directory level. DECISIONS #30 is explicit that naming
these 6 URLs does not pre-authorize that.

Live recon (2026-08-20) found the 6 pages are **not** one shared template -
three distinct shapes:

- **Pure hub pages** (business.php, community.php, housing.php,
  youth.php): the exact same ``div.v2-tiles-con`` / ``div.v2-tile`` tile
  grid as the index page (DECISIONS #27/#28) - each tile carries only a
  sub-program name + link, no per-item description (``v2-tile-caption`` is
  empty on every tile observed across all 4 pages) and no date/amount.
  The real per-program data (grant amounts, deadlines, eligibility)
  confirmed live to live one directory level deeper still (e.g.
  ``for_business_owners.php``, ``arts_grants_program.php``,
  ``affordable_housing_lot_disposition_program.php``,
  ``community_impact_summer_enhancement_grant.php``) - DECISIONS #30's
  hard stop applies: not followed. See DECISIONS #32 - this confirms and
  extends DECISIONS #27's single-page suspicion to all 4 of these pages.
  Tile parsing reuses ``StpeteGrantsCrawler.parse_tiles_page()`` since the
  markup is byte-for-byte the same template (see DECISIONS #32).

- **Freeform content page** (for_south_stpete.php): no tile grid at all.
  Real per-program content lives directly on the page: an ``<h1>``
  overview heading, an "Overview" ``<h2>`` + intro ``<p>``, then repeated
  blocks of an ``<h3>`` (or occasionally a nested ``<h4>`` sub-program)
  program-name heading, one or more ``<p>`` description paragraphs, and 0+
  "More Info"/application links (each inside a ``<p><span class="btn">``),
  with each block separated by an ``<hr>``. No dollar amounts or
  application deadlines appear anywhere in the page's plain text. See
  DECISIONS #33.

- **Tile-with-details page** (sunrise_st._pete/index.php): the same
  ``div.v2-tiles-con`` tile grid as the hub pages, but each tile's
  ``.v2-tile-info`` additionally carries an eligibility line
  (``<p><em>...</em></p>``) and a plain description ``<p>`` beyond the
  still-empty ``.v2-tile-caption`` - real per-program detail embedded
  directly in the tile, confirmed identical shape across all 5 tiles
  observed. The page also states a program-wide total funding figure
  ("$159.8 million in federal funding") in its intro prose, but that is
  page-level context, not a per-program figure, and is not extracted as
  structured data here. See DECISIONS #34.
"""

from __future__ import annotations

from app.crawlers.base import BaseCrawler
from app.crawlers.stpete_grants import StpeteGrantCategory, StpeteGrantsCrawler

# --- The 6 URLs, exactly as named in DECISIONS #30 --------------------------

BUSINESS_URL = "https://www.stpete.org/residents/grants___loans/business.php"
COMMUNITY_URL = "https://www.stpete.org/residents/grants___loans/community.php"
HOUSING_URL = "https://www.stpete.org/residents/grants___loans/housing.php"
SOUTH_STPETE_URL = "https://www.stpete.org/residents/grants___loans/for_south_stpete.php"
YOUTH_URL = "https://www.stpete.org/residents/grants___loans/youth.php"
SUNRISE_URL = "https://www.stpete.org/residents/grants___loans/sunrise_st._pete/index.php"

# The 4 pure-hub pages, confirmed live (2026-08-20) to share the index
# page's tile-grid template with no per-item description or date - see
# module docstring / DECISIONS #32.
HUB_PAGE_URLS: dict[str, str] = {
    "For Business": BUSINESS_URL,
    "For Community & Neighborhoods": COMMUNITY_URL,
    "For Housing": HOUSING_URL,
    "For Youth": YOUTH_URL,
}


class StpeteGrantCategoryPagesCrawler(BaseCrawler):
    """Crawls DECISIONS #30's 6 category sub-pages. Split into three
    methods, one per confirmed page shape (see module docstring) - there is
    no single ``crawl()`` that returns one uniform item type because the
    live pages themselves aren't uniform."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_grant_categories", **kwargs)
        # Reused only for its parse_tiles_page() tile-grid parser (pure HTML
        # parsing, no I/O) - see DECISIONS #32. This inner crawler's own
        # fetch()/crawl()/rate-limiter/robots state is never used; all
        # fetching for this module goes through self.fetch() above, so
        # there's no per-host throttling state to keep in sync.
        self._tiles_parser = StpeteGrantsCrawler(min_request_interval_seconds=0)

    def crawl_hub_pages(self) -> dict[str, list[StpeteGrantCategory]]:
        """Fetches all 4 pure-hub pages. Returns a dict keyed by page URL,
        each value the list of sub-program tile links found on that page.
        These are link-only items (name + url + optional caption, no
        amount/deadline) because that's genuinely all that's present on
        these 4 pages - see module docstring / DECISIONS #32."""
        results: dict[str, list[StpeteGrantCategory]] = {}
        for url in HUB_PAGE_URLS.values():
            resp = self.fetch(url)
            results[url] = self._tiles_parser.parse_tiles_page(resp.text, url)
        return results
