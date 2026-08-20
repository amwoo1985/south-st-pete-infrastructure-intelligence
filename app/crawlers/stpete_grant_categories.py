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
  blocks of an ``<h3>`` (or, for 2 of the 7 programs, a nested ``<h4>``
  sub-program) program-name heading, one or more ``<p>`` description
  paragraphs, and 0+ "More Info"/application links (each inside a
  ``<p><span class="btn">``), with each block usually separated by an
  ``<hr>``. No dollar amounts or application deadlines appear anywhere in
  the page's plain text. See DECISIONS #33.

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

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler
from app.crawlers.stpete_grants import (
    TILE_CAPTION_CLASS,
    TILE_CLASS,
    TILE_LINK_CLASS,
    TILES_CONTAINER_CLASS,
    StpeteGrantCategory,
    StpeteGrantsCrawler,
)

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

# for_south_stpete.php's real content lives inside this one selector -
# confirmed live (2026-08-20) to match exactly once on the page, containing
# the <h1> overview heading and every program block. See DECISIONS #33.
SOUTH_STPETE_CONTENT_SELECTOR = "#post .module-container"

# A "links paragraph" on for_south_stpete.php (a <p> whose only content is
# a "More Info"/application link, wrapped in <span class="btn">) - its <a>
# href(s) are collected as detail_urls, and its text is not added to the
# program's description since it duplicates the link text. See DECISIONS #33.
_LINKS_PARAGRAPH_MARKER_CLASS = "btn"


@dataclass(frozen=True)
class StpeteGrantProgramDetail:
    """One program block from for_south_stpete.php - headed by an <h3> or
    (for a nested sub-program) <h4>, per DECISIONS #33."""

    program_name: str
    description: str | None
    detail_urls: tuple[str, ...]
    attribution: Attribution


@dataclass(frozen=True)
class StpeteSunriseProgram:
    """One "Active Programs" tile from sunrise_st._pete/index.php. Unlike
    the 4 pure-hub pages, this page's tiles carry real per-program detail
    (an eligibility line + a description paragraph) directly in the tile
    markup, beyond the still-empty .v2-tile-caption - see DECISIONS #34."""

    program_name: str
    program_url: str
    eligibility: str | None
    description: str | None
    attribution: Attribution


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

    def crawl_south_stpete_page(self) -> list[StpeteGrantProgramDetail]:
        resp = self.fetch(SOUTH_STPETE_URL)
        return self.parse_south_stpete_page(resp.text)

    def parse_south_stpete_page(self, html: str) -> list[StpeteGrantProgramDetail]:
        """Parses for_south_stpete.php's freeform content - see module
        docstring / DECISIONS #33. Unlike the other 5 pages, there is no
        tile grid here: real per-program content is a flat sequence of
        <h3>/<h4> program headings, description <p>s, and "More Info"/
        application links, each block separated by an <hr>."""
        soup = BeautifulSoup(html, "lxml")

        base_tag = soup.find("base")
        link_base = (
            base_tag.get("href") if base_tag is not None and base_tag.get("href") else SOUTH_STPETE_URL
        )

        container = soup.select_one(SOUTH_STPETE_CONTENT_SELECTOR)
        if container is None:
            self.fail_loud(
                f"content container {SOUTH_STPETE_CONTENT_SELECTOR!r} not found on {SOUTH_STPETE_URL} "
                "- stpete.org's grants/loans page structure may have changed"
            )

        if container.find("h1") is None:
            self.fail_loud(
                f"content container {SOUTH_STPETE_CONTENT_SELECTOR!r} has no <h1> heading "
                f"on {SOUTH_STPETE_URL}"
            )

        retrieval_time = datetime.now(timezone.utc)

        programs: list[StpeteGrantProgramDetail] = []
        current_name: str | None = None
        current_paragraphs: list[str] = []
        current_links: list[str] = []

        def flush() -> None:
            if current_name is None:
                return
            description = " ".join(p for p in current_paragraphs if p).strip() or None
            # Dedupe while preserving order - the same "More Info" link
            # occasionally repeats across a block's paragraphs.
            detail_urls = tuple(dict.fromkeys(current_links))
            programs.append(
                StpeteGrantProgramDetail(
                    program_name=current_name,
                    description=description,
                    detail_urls=detail_urls,
                    attribution=Attribution(
                        source_url=SOUTH_STPETE_URL,
                        retrieval_timestamp=retrieval_time,
                        # No per-program date appears anywhere in this
                        # page's plain text (confirmed live) - nullable per
                        # .claude/rules/data.md, not a sentinel.
                        published_date=None,
                    ),
                )
            )

        for el in container.find_all(["h3", "h4", "p", "hr"], recursive=True):
            if el.name in ("h3", "h4"):
                flush()
                current_name = el.get_text(strip=True)
                current_paragraphs = []
                current_links = []
            elif el.name == "p":
                if current_name is None:
                    # Prose before the first program heading (the page's
                    # "Overview" intro) - not a program block.
                    continue
                if el.find("span", class_=_LINKS_PARAGRAPH_MARKER_CLASS) is not None:
                    for a in el.find_all("a", href=True):
                        current_links.append(urljoin(link_base, a["href"]))
                else:
                    text = el.get_text(" ", strip=True)
                    if text:
                        current_paragraphs.append(text)
            elif el.name == "hr":
                flush()
                current_name = None
                current_paragraphs = []
                current_links = []
        flush()

        if not programs:
            self.fail_loud(
                f"no <h3>/<h4> program headings found in {SOUTH_STPETE_CONTENT_SELECTOR!r} "
                f"on {SOUTH_STPETE_URL} - page content may have changed shape"
            )

        return programs

    def crawl_sunrise_page(self) -> list[StpeteSunriseProgram]:
        resp = self.fetch(SUNRISE_URL)
        return self.parse_sunrise_page(resp.text)

    def parse_sunrise_page(self, html: str) -> list[StpeteSunriseProgram]:
        """Parses sunrise_st._pete/index.php's "Active Programs" tile grid -
        see module docstring / DECISIONS #34. Same div.v2-tiles-con/div.v2-tile
        markup as the 4 hub pages, but each tile's .v2-tile-info carries 2
        extra <p> elements beyond the (still-empty) .v2-tile-caption: one
        wrapping an <em> eligibility line, one plain description paragraph -
        confirmed live, identical shape across all 5 tiles observed."""
        soup = BeautifulSoup(html, "lxml")

        base_tag = soup.find("base")
        link_base = base_tag.get("href") if base_tag is not None and base_tag.get("href") else SUNRISE_URL

        container = soup.find("div", class_=TILES_CONTAINER_CLASS)
        if container is None:
            self.fail_loud(
                f"tiles container div.{TILES_CONTAINER_CLASS} not found on {SUNRISE_URL} "
                "- stpete.org's grants/loans page structure may have changed"
            )

        tiles = container.find_all("div", class_=TILE_CLASS, recursive=False)
        if not tiles:
            self.fail_loud(
                f"no div.{TILE_CLASS} program cards found inside div.{TILES_CONTAINER_CLASS} "
                f"on {SUNRISE_URL}"
            )

        retrieval_time = datetime.now(timezone.utc)

        programs: list[StpeteSunriseProgram] = []
        for tile in tiles:
            programs.append(self._parse_sunrise_tile(tile, retrieval_time, link_base))
        return programs

    def _parse_sunrise_tile(self, tile: Tag, retrieval_time, link_base: str) -> StpeteSunriseProgram:
        info = tile.find("div", class_="v2-tile-info")
        if info is None:
            self.fail_loud(
                f"a div.{TILE_CLASS} card has no div.v2-tile-info - "
                f"tile markup: {tile.get('class')} on {SUNRISE_URL}"
            )

        link = info.find("a", class_=TILE_LINK_CLASS)
        if link is None:
            self.fail_loud(f"a div.v2-tile-info has no a.{TILE_LINK_CLASS} on {SUNRISE_URL}")
        href = link.get("href")
        if not href:
            self.fail_loud(f"a.{TILE_LINK_CLASS} has no href on {SUNRISE_URL}")

        program_name = link.get_text(strip=True)
        if not program_name:
            self.fail_loud(f"a.{TILE_LINK_CLASS} has empty text on {SUNRISE_URL}")

        program_url = urljoin(link_base, href)

        # The always-empty .v2-tile-caption (same as the 4 hub pages) plus
        # 0-2 extra <p> elements carrying the real per-program detail on
        # this page specifically. Classify by whether a <p> wraps an <em>
        # (eligibility) or not (description) - nullable when a tile omits
        # one, per .claude/rules/data.md, not a structure failure: DECISIONS
        # #32's hub pages prove a caption-only tile is a legitimate stpete.org
        # state, not necessarily broken markup.
        extra_paragraphs = [
            p for p in info.find_all("p", recursive=False) if TILE_CAPTION_CLASS not in (p.get("class") or [])
        ]

        eligibility: str | None = None
        description_parts: list[str] = []
        for p in extra_paragraphs:
            text = p.get_text(" ", strip=True)
            if not text:
                continue
            if p.find("em") is not None:
                eligibility = text
            else:
                description_parts.append(text)
        description = " ".join(description_parts) or None

        attribution = Attribution(
            source_url=SUNRISE_URL,
            retrieval_timestamp=retrieval_time,
            # No per-program date appears in any tile's text (confirmed
            # live) - nullable per .claude/rules/data.md, not a sentinel.
            published_date=None,
        )

        return StpeteSunriseProgram(
            program_name=program_name,
            program_url=program_url,
            eligibility=eligibility,
            description=description,
            attribution=attribution,
        )
