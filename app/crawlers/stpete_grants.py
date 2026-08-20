"""City of St. Petersburg grants/loans, index page only.

The only stpete.org source, per DECISIONS #11 — do not add any other
stpete.org page here without a new DECISIONS.md entry first.

Target (exact page named in DECISIONS #11):
    https://stpete.org/residents/grants___loans/index.php

Live-site recon (2026-08-20): the bare ``stpete.org`` host 301-redirects to
``www.stpete.org`` (which serves the real content); ``requests`` follows
redirects transparently, so fetching the bare-domain URL named in
DECISIONS #11 works without any change to ``ALLOWED_SOURCE_HOSTS`` — see
DECISIONS #26.

Recon also confirmed the index page's content is exactly PLAN.md's "6
categories": a ``<div class="v2-tiles-con">`` containing six
``<div class="v2-tile">`` cards, one per grants/loans category (For
Business, For Community & Neighborhoods, For Housing, For South St. Pete
CRA, For Youth, Sunrise St. Pete). Each tile carries only a category name
and a link to a category sub-page — no per-item description text (the
``<p class="v2-tile-caption">`` is empty for every tile observed) and no
date. The actual per-program grant details (amounts, deadlines, effective
dates) live one or more directory levels deeper (e.g.
``residents/grants___loans/business.php``, itself another hub page linking
to ``for_business_owners.php`` etc.) — out of DECISIONS #11's exact-page
scope. See DECISIONS #27: this is flagged as a blocker, not followed.

The page's ``<head>`` declares ``<base href="https://www.stpete.org/" />``.
Every tile's ``href`` is site-root-relative (e.g.
``residents/grants___loans/business.php``), meaning it resolves against
that ``<base>``, not against the fetched page's own URL/directory —
resolving against the page URL instead would silently produce a doubled,
broken path (confirmed live: naive ``urljoin(GRANTS_URL, href)`` produced
``.../grants___loans/residents/grants___loans/business.php``). See
DECISIONS #28.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler

GRANTS_URL = "https://stpete.org/residents/grants___loans/index.php"

TILES_CONTAINER_CLASS = "v2-tiles-con"
TILE_CLASS = "v2-tile"
TILE_LINK_CLASS = "v2-tile-link"
TILE_CAPTION_CLASS = "v2-tile-caption"


@dataclass(frozen=True)
class StpeteGrantCategory:
    category_name: str
    category_url: str
    description: str | None  # None when the tile's caption is empty (the observed live state)
    attribution: Attribution


class StpeteGrantsCrawler(BaseCrawler):
    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_grants", **kwargs)

    def crawl(self) -> list[StpeteGrantCategory]:
        resp = self.fetch(GRANTS_URL)
        return self.parse_index(resp.text)

    def parse_index(self, html: str) -> list[StpeteGrantCategory]:
        return self.parse_tiles_page(html, GRANTS_URL)

    def parse_tiles_page(self, html: str, page_url: str) -> list[StpeteGrantCategory]:
        """Parses the shared ``div.v2-tiles-con`` / ``div.v2-tile`` tile-grid
        template against any stpete.org page that uses it - not just this
        module's own index page. DECISIONS #30's business.php/community.php/
        housing.php/youth.php sub-pages (see
        ``app/crawlers/stpete_grant_categories.py``) use the exact same
        markup, just with a different ``page_url``, so this is factored out
        rather than duplicated - see DECISIONS #32."""
        soup = BeautifulSoup(html, "lxml")

        # Relative hrefs on this page resolve against the document's
        # declared <base>, not against page_url's own directory - see
        # module docstring / DECISIONS #28. Fall back to page_url if a
        # future page revision drops the <base> tag.
        base_tag = soup.find("base")
        link_base = base_tag.get("href") if base_tag is not None and base_tag.get("href") else page_url

        container = soup.find("div", class_=TILES_CONTAINER_CLASS)
        if container is None:
            self.fail_loud(
                f"tiles container div.{TILES_CONTAINER_CLASS} not found on {page_url} "
                "- stpete.org's grants/loans page structure may have changed"
            )

        tiles = container.find_all("div", class_=TILE_CLASS, recursive=False)
        if not tiles:
            self.fail_loud(
                f"no div.{TILE_CLASS} category cards found inside div.{TILES_CONTAINER_CLASS} "
                f"on {page_url}"
            )

        # One retrieval timestamp for the whole page fetch, shared by every
        # category parsed from it - mirrors legistar.py's pattern.
        retrieval_time = datetime.now(timezone.utc)

        categories: list[StpeteGrantCategory] = []
        for tile in tiles:
            categories.append(self._parse_tile(tile, retrieval_time, link_base, page_url))
        return categories

    def _parse_tile(self, tile: Tag, retrieval_time, link_base: str, page_url: str = GRANTS_URL) -> StpeteGrantCategory:
        link = tile.find("a", class_=TILE_LINK_CLASS)
        if link is None:
            self.fail_loud(
                f"a div.{TILE_CLASS} card has no a.{TILE_LINK_CLASS} - "
                f"tile markup: {tile.get('class')}"
            )
        href = link.get("href")
        if not href:
            self.fail_loud(f"a.{TILE_LINK_CLASS} has no href on {page_url}")

        category_name = link.get_text(strip=True)
        if not category_name:
            self.fail_loud(f"a.{TILE_LINK_CLASS} has empty text on {page_url}")

        category_url = urljoin(link_base, href)

        caption_el = tile.find("p", class_=TILE_CAPTION_CLASS)
        caption_text = caption_el.get_text(strip=True) if caption_el is not None else ""
        description = caption_text or None

        # No per-item date is present at the index-page level (see module
        # docstring) - nullable per .claude/rules/data.md, not a magic
        # sentinel.
        attribution = Attribution(
            source_url=page_url,
            retrieval_timestamp=retrieval_time,
            published_date=None,
        )

        return StpeteGrantCategory(
            category_name=category_name,
            category_url=category_url,
            description=description,
            attribution=attribution,
        )
