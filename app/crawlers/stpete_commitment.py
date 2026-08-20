"""stpete.org DECISIONS #46 st_petes_commitment.php - the City's American
Cities Climate Challenge (ACCC) action-item work plan, including the named
"Implement first Duke Energy community solar for energy equity benefiting
low income area" Moonshot item DECISIONS #46 was added for.

Scope is exactly this 1 URL DECISIONS #46 names - do not add another one
without a new DECISIONS.md entry, and do not follow any link found on this
page to a further page. DECISIONS #46 is explicit this does not reopen a
general sweep of ``stpete.org/residents/sustainability/``.

Live recon (2026-08-20) confirmed this page does **not** share
``app/crawlers/stpete_program_details.py``'s single-container ``<h1>``/
``<h2>``/``<h3>`` template, and does **not** share
``app/crawlers/stpete_arpa.py``'s "by the #s" ``<strong>``-amount-paragraph
template either. ``#post .module-container`` matches 5 separate top-level
blocks (a heading block, two FAQ-accordion content blocks, and 2 more small
heading blocks for "A 4-STAR Community & LEED Certified City" and "Envision
Award Winner") - not one container holding the page's content in document
order. The page's own visible ``<h1>`` ("St. Pete's Commitment") lives
outside ``#post`` entirely, in a banner/slider caption
(``section.slider-wrap div.inner-slider-caption``) - it is not part of the
``#post`` content region at all, unlike every prior stpete.org page in this
crawl chain.

The real action-item content lives inside one of the FAQ-accordion blocks
(``div.faq-container div.faq-item div.faq-answer``), itself a single
expandable "More" FAQ entry - not a real question/answer pair, just this
CMS's mechanism for a collapsible content block. Inside that one
``.faq-answer``, in document order: an ``<h3>`` "What St. Pete Received"
(a ``<ul>`` of the Bloomberg Philanthropies ACCC support-package items, no
per-item structure worth modeling - general narrative, not action items),
an ``<h3>`` "Accelerating St. Pete's Climate Action" followed by one
``<p>`` naming the page-level "reduce ... GHG emissions 20% by 2020" target
and the Foundational (F) / Ambitious (A) / Moonshot (M) tier scheme, and
then exactly 2 ``<h4>`` sector headings - "Building Sector" (10 items) and
"Transportation Sector" (6 items), confirmed the only 2 ``<h4>`` anywhere
on the page - each followed by a ``<ul>`` of action items. Every ``<li>``
ends in a parenthetical tier marker, ``(F)``, ``(A)``, or ``(M)``, which
this parser splits out into a typed ``tier_code``/``tier_label`` pair
rather than leaving it embedded in prose only, while keeping the full
verbatim ``<li>`` text (marker included) as ``text``. This mirrors
DECISIONS #45's ``ArpaFundingAllocation`` shape: a bespoke, targeted parser
keyed on the one real structural signal this page has, rather than forcing
it through either existing template.

The Duke Energy community solar item is the last of the 10 Building Sector
items, tier ``(M)`` Moonshot, exactly as DECISIONS #46's own recon
described - confirmed live, not merely quoted secondhand. No dollar amount
or per-item date is present for it or any other action item; the only
date-shaped text on the page is the page-level "20% by 2020" emissions
target named once in the "Accelerating St. Pete's Climate Action" intro
paragraph, not attributable to any single item (same "don't promote a
page-level fact to a per-item date" discipline as DECISIONS #37/#42/#45).
Every item's ``published_date`` is ``None`` - nullable per
``.claude/rules/data.md``, not a sentinel.

Two Building Sector items carry an inline link (the residential solar
co-op item links to ``solarunitedneighbors.org``, off-host; the SELF
financing item links to ``solarenergyloanfund.org``, off-host); one
Transportation Sector item links to a relative
``visitors/scooter_safety.php`` path, resolved against the page's own
``<base>``/URL and captured on-host. All links are captured verbatim in
``links``, never fetched - same hard-stop discipline as every prior round.
The Duke Energy item itself carries no link.

The page's other content - the Bloomberg support-package bullet list, the
2019 STAR Certification Results table, and the Envision Award section -
is real but a different structure (a plain bullet list and a data table,
not F/A/M-tiered action items) and not what DECISIONS #46 was added for;
it is not modeled here, same "don't force unrelated content into this
shape" discipline as DECISIONS #45's un-modeled FAQ/news blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler

# --- The 1 URL DECISIONS #46 names, exactly ---------------------------------

COMMITMENT_URL = "https://www.stpete.org/residents/sustainability/st_petes_commitment.php"

_CONTENT_CONTAINER_SELECTOR = "#post .module-container"

# Every action-item sector on this page is an <h4> whose immediately
# following sibling is the <ul> of that sector's items - confirmed live
# (2026-08-20) to match exactly 2 headings ("Building Sector", "Transportation
# Sector") and no others anywhere on the page. See module docstring.
_TIER_LABELS = {
    "F": "Foundational",
    "A": "Ambitious",
    "M": "Moonshot",
}

# Matches a <li>'s verbatim text ending in "(F)", "(A)", or "(M)" - the one
# structural signal marking a real action item vs. any other list on the
# page.
_TIER_MARKER_RE = re.compile(r"\(([FAM])\)\s*$")

# The exact item text DECISIONS #46 was added for - asserted present after
# parsing, not just assumed from the entry's own recon quote. See module
# docstring / DECISIONS #47.
_DUKE_ENERGY_ITEM_SUBSTRING = "Duke Energy community solar"


@dataclass(frozen=True)
class StpeteCommitmentActionItem:
    """One <li> action item under a sector's <h4> heading, on
    st_petes_commitment.php's ACCC work plan. `text` is the full verbatim
    <li> text, tier marker included; `tier_code`/`tier_label` are that same
    marker split out into a typed field."""

    sector: str
    text: str
    tier_code: str
    tier_label: str
    links: tuple[str, ...]
    attribution: Attribution


class StpeteCommitmentCrawler(BaseCrawler):
    """Crawls DECISIONS #46's st_petes_commitment.php action-item work
    plan. See module docstring."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_commitment", **kwargs)

    def crawl(self) -> list[StpeteCommitmentActionItem]:
        resp = self.fetch(COMMITMENT_URL)
        return self.parse_commitment_page(resp.text, COMMITMENT_URL)

    def parse_commitment_page(self, html: str, page_url: str) -> list[StpeteCommitmentActionItem]:
        soup = BeautifulSoup(html, "lxml")

        base_tag = soup.find("base")
        link_base = base_tag.get("href") if base_tag is not None and base_tag.get("href") else page_url

        containers = soup.select(_CONTENT_CONTAINER_SELECTOR)
        if not containers:
            self.fail_loud(
                f"no {_CONTENT_CONTAINER_SELECTOR!r} content blocks found on {page_url} "
                "- stpete.org's commitment page structure may have changed"
            )

        sector_headings: list[Tag] = []
        for container in containers:
            sector_headings.extend(container.find_all("h4"))

        if not sector_headings:
            self.fail_loud(
                f"no sector heading (<h4>) found in {_CONTENT_CONTAINER_SELECTOR!r} on {page_url} "
                "- the Building Sector/Transportation Sector action-item structure may have changed"
            )

        retrieval_time = datetime.now(timezone.utc)

        items: list[StpeteCommitmentActionItem] = []
        for h4 in sector_headings:
            sector = h4.get_text(strip=True)
            if not sector:
                self.fail_loud(f"a sector <h4> on {page_url} has empty text")

            ul = h4.find_next_sibling("ul")
            if ul is None:
                self.fail_loud(f"sector {sector!r} on {page_url} has no <ul> of action items following it")

            list_items = ul.find_all("li", recursive=False)
            if not list_items:
                self.fail_loud(f"sector {sector!r} on {page_url} has an empty action-item <ul>")

            for li in list_items:
                items.append(self._parse_action_item(li, sector, page_url, link_base, retrieval_time))

        if not items:
            self.fail_loud(f"no action items parsed from {page_url}")

        if not any(_DUKE_ENERGY_ITEM_SUBSTRING in item.text for item in items):
            self.fail_loud(
                f"expected action item containing {_DUKE_ENERGY_ITEM_SUBSTRING!r} not found on {page_url} "
                "- this is the item DECISIONS #46 named this page for; its absence signals real content "
                "drift, not just a cosmetic markup change"
            )

        return items

    def _parse_action_item(
        self, li: Tag, sector: str, page_url: str, link_base: str, retrieval_time: datetime
    ) -> StpeteCommitmentActionItem:
        text = li.get_text(" ", strip=True)
        if not text:
            self.fail_loud(f"an action item <li> under sector {sector!r} on {page_url} has no text")

        match = _TIER_MARKER_RE.search(text)
        if not match:
            self.fail_loud(
                f"action item {text!r} under sector {sector!r} on {page_url} has no trailing (F)/(A)/(M) "
                "tier marker - stpete.org's tier scheme may have changed"
            )
        tier_code = match.group(1)
        tier_label = _TIER_LABELS[tier_code]

        links = tuple(dict.fromkeys(urljoin(link_base, a["href"]) for a in li.find_all("a", href=True)))

        return StpeteCommitmentActionItem(
            sector=sector,
            text=text,
            tier_code=tier_code,
            tier_label=tier_label,
            links=links,
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                # No per-item date exists on the page - see module
                # docstring. Nullable per .claude/rules/data.md, not a
                # guessed sentinel.
                published_date=None,
            ),
        )
