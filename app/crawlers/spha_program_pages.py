"""St. Petersburg Housing Authority (`www.stpeteha.org`), DECISIONS #48/#49's
8 "program info" pages:

    https://www.stpeteha.org/housing
    https://www.stpeteha.org/public-housing-clients
    https://www.stpeteha.org/affordable-housing-clients
    https://www.stpeteha.org/section-8-hcv-voucher-holder
    https://www.stpeteha.org/fss-program
    https://www.stpeteha.org/homeownership
    https://www.stpeteha.org/annual-plans
    https://www.stpeteha.org/performance-report

Scope is exactly these 8 URLs (the 9th DECISIONS #48 page, `/news`, is a
different shape entirely - an index, see `app/crawlers/spha_news.py`). Do
not add another `stpeteha.org` page here without a new DECISIONS.md entry
first, and do not follow any link found on these pages.

Live recon (2026-08-20) confirmed this is a completely different CMS from
every `stpete.org` page in this crawl chain - not WordPress, not the
`#post .module-container`/`<h1>`/`<h2>`/`<h3>` template DECISIONS
#33/#35/#41 built for, and not a `div.v2-tiles-con` tile grid either
(DECISIONS #27/#30/#32/#34). It's a custom PHP CMS (`templates/
stpeteha.org/...` asset paths, `/plugins/show_image.php` image proxies, no
`<base>` tag). Every one of these 8 pages shares one real template: a page
title in `<h1 class="ptitles">` (outside the scrollable content region),
and the actual content in a single `<div id="bodyContainer">` holding
freeform WYSIWYG-editor markup (`<p>`/`<ul>`/`<ol>`/`<hr>`/occasionally a
promo `<div>`) - no real `<h2>`/`<h3>` tags anywhere in it.

The one real, consistently-used section-boundary signal in that freeform
markup is a `<span style="font-size: 36px;">` wrapping a `<strong>` with no
`<a href>` inside it - confirmed live to mark every genuine section heading
across all 8 pages ("FAQ", "Paying Rent", "Eligibility", "Renting a Unit",
"Annual Plans", ...). The same 36px/bold styling is reused elsewhere on
these pages for large CTA buttons (`<span style="font-size: 36px;">
<strong><a href="...">CLICK HERE...</a></strong></span>`, confirmed live on
`public-housing-clients` and `affordable-housing-clients`) - those are
excluded by the `no <a href>` check, otherwise every "CLICK HERE to pay
rent" button would be misparsed as its own section. `section-8-hcv-
voucher-holder`'s headings additionally each wrap an empty in-page-jump
`<a id="...">` anchor (`<a id="RentingaUnit"></a>Renting a Unit`) with no
`href` - those correctly still count as headings, since the "no link"
check is `find("a", href=True)`, not "no `<a>` tag at all".

A section's `text` is every block's text between one heading and the next
(or, for content before the first heading, a `heading=None` "lead" section
- `performance-report` has no 36px headings at all, so its entire content
is one `heading=None` section; every other page has at least one). No
attempt is made to further parse FAQ-style Q&A pairs out of section text -
live recon found this pattern implemented 3 different, inconsistent ways
across pages (inline `<li><strong>Q</strong><br/>A</li>`, nested
`<li><strong>Q</strong></li><ul><li>A</li></ul>`, and plain narrative
paragraphs with no Q/A markup at all) - forcing one shape onto all three
would silently mismatch at least two of them, the same "don't force a
shape that isn't cleanly and consistently there" call DECISIONS #45 made
for the ARPA page's un-modeled FAQ block. Section text is kept as raw
prose instead - eligibility percentages ("up to 80 percent of the Area
Median Income", "140% AMI"), dollar-adjacent program terms, and dates
embedded in prose (e.g. `annual-plans`' "accepted through September 24,
2026" comment deadline) are all present verbatim in `text`, same as
`StpeteProgramSection.text` (DECISIONS #35).

`homeownership` also contains one stray, real `<h1 class="font_0
wixui-rich-text__text">` mid-content (a realtor's name, "Archer Realty
Solutions" - a Wix-CMS class name, clearly pasted in from another site's
formatted content, not a page-level heading) - it's included in the block
walk as ordinary content (captured into the enclosing section's `text`,
same as any other paragraph) rather than treated as a heading signal, so
its text isn't silently dropped.

No dollar amount (e.g. DECISIONS #48's own recon-quoted $842K HUD Capital
Fund award, $104K FSS grant) was confirmed live on any of these 8 pages -
see `app/crawlers/spha_news.py`'s module docstring for where that content
actually lives and why it's out of this round's scope.

Every section's `published_date` is not modeled per-item; see
`SphaProgramPage`/`SphaProgramSection` below - `Attribution.published_date`
stays `None` at the page level (nullable per `.claude/rules/data.md`),
matching DECISIONS #35/#45's "don't guess which embedded date is the
effective date" discipline. Raw dates remain in `text`, verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler

# --- The 8 URLs DECISIONS #48 names, exactly (minus /news - see module docstring) ---

HOUSING_URL = "https://www.stpeteha.org/housing"
PUBLIC_HOUSING_CLIENTS_URL = "https://www.stpeteha.org/public-housing-clients"
AFFORDABLE_HOUSING_CLIENTS_URL = "https://www.stpeteha.org/affordable-housing-clients"
SECTION_8_HCV_VOUCHER_HOLDER_URL = "https://www.stpeteha.org/section-8-hcv-voucher-holder"
FSS_PROGRAM_URL = "https://www.stpeteha.org/fss-program"
HOMEOWNERSHIP_URL = "https://www.stpeteha.org/homeownership"
ANNUAL_PLANS_URL = "https://www.stpeteha.org/annual-plans"
PERFORMANCE_REPORT_URL = "https://www.stpeteha.org/performance-report"

PROGRAM_PAGE_URLS: tuple[str, ...] = (
    HOUSING_URL,
    PUBLIC_HOUSING_CLIENTS_URL,
    AFFORDABLE_HOUSING_CLIENTS_URL,
    SECTION_8_HCV_VOUCHER_HOLDER_URL,
    FSS_PROGRAM_URL,
    HOMEOWNERSHIP_URL,
    ANNUAL_PLANS_URL,
    PERFORMANCE_REPORT_URL,
)

_TITLE_SELECTOR = "h1.ptitles"
_CONTENT_CONTAINER_SELECTOR = "#bodyContainer"

# Block-level tags collected within #bodyContainer, in document order. See
# module docstring - this freeform CMS has no real <h2>/<h3>, so these are
# the only real content boundaries. `h1` is included only to capture the
# one stray mid-content <h1> found on `homeownership` as ordinary text (see
# module docstring); it is never treated as a page title here (page title
# always comes from `_TITLE_SELECTOR`, scoped to the `ptitles` class).
_BLOCK_TAGS = ("p", "ul", "ol", "div", "hr", "h1")


def _is_top_level_block(el: Tag, container: Tag) -> bool:
    """True unless `el` is itself nested inside another `_BLOCK_TAGS`
    element within `container` (avoids double-counting a <p>/<ul> nested
    inside a promo <div>, same discipline as DECISIONS #35's
    `_is_top_level_block`)."""
    for ancestor in el.parents:
        if ancestor is container:
            return True
        if getattr(ancestor, "name", None) in _BLOCK_TAGS:
            return False
    return True


def _is_heading_span(span: Tag) -> bool:
    """True for a real section-heading span: font-size:36px, wraps a
    <strong>, and contains no <a href> (excludes the CTA-button reuse of
    the same 36px/bold styling - see module docstring). An empty in-page
    jump-target anchor with no href, e.g. `<a id="FAQ">`, does not
    disqualify a heading."""
    style = span.get("style") or ""
    if "font-size: 36px" not in style:
        return False
    if span.find("strong") is None:
        return False
    if span.find("a", href=True) is not None:
        return False
    return True


@dataclass(frozen=True)
class SphaProgramSection:
    """One heading-delimited block of content within a DECISIONS #48
    program page's `#bodyContainer`. `heading` is `None` for the lead
    content before the first real heading (or the page's only section, on
    a page with no 36px headings at all - confirmed live on
    `performance-report`)."""

    heading: str | None
    text: str | None
    links: tuple[str, ...]


@dataclass(frozen=True)
class SphaProgramPage:
    """One of the 8 DECISIONS #48 `stpeteha.org` program pages."""

    page_title: str
    page_url: str
    sections: tuple[SphaProgramSection, ...]
    attribution: Attribution


class SphaProgramPagesCrawler(BaseCrawler):
    """Crawls the 8 DECISIONS #48 `stpeteha.org` program pages. See module
    docstring."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="spha_program_pages", **kwargs)

    def crawl(self) -> dict[str, SphaProgramPage]:
        """Fetches all 8 pages. Returns a dict keyed by page URL."""
        results: dict[str, SphaProgramPage] = {}
        for url in PROGRAM_PAGE_URLS:
            resp = self.fetch(url)
            results[url] = self.parse_program_page(resp.text, url)
        return results

    def parse_program_page(self, html: str, page_url: str) -> SphaProgramPage:
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one(_TITLE_SELECTOR)
        if title_el is None:
            self.fail_loud(
                f"no {_TITLE_SELECTOR!r} page title found on {page_url} - "
                "stpeteha.org's page-title markup may have changed"
            )
        page_title = title_el.get_text(strip=True)
        if not page_title:
            self.fail_loud(f"{_TITLE_SELECTOR!r} has empty text on {page_url}")

        container = soup.select_one(_CONTENT_CONTAINER_SELECTOR)
        if container is None:
            self.fail_loud(
                f"content container {_CONTENT_CONTAINER_SELECTOR!r} not found on {page_url} - "
                "stpeteha.org's page structure may have changed"
            )

        blocks = [
            el
            for el in container.find_all(list(_BLOCK_TAGS), recursive=True)
            if _is_top_level_block(el, container)
        ]
        if not blocks:
            self.fail_loud(f"content container {_CONTENT_CONTAINER_SELECTOR!r} on {page_url} has no content blocks")

        retrieval_time = datetime.now(timezone.utc)

        sections: list[SphaProgramSection] = []
        cur_heading: str | None = None
        cur_text_parts: list[str] = []
        cur_links: list[str] = []

        def flush() -> None:
            nonlocal cur_heading, cur_text_parts, cur_links
            text = " ".join(t for t in cur_text_parts if t).strip() or None
            if text or cur_links or cur_heading is not None:
                sections.append(
                    SphaProgramSection(
                        heading=cur_heading,
                        text=text,
                        links=tuple(dict.fromkeys(cur_links)),
                    )
                )
            cur_heading = None
            cur_text_parts = []
            cur_links = []

        for block in blocks:
            if block.name == "hr":
                continue

            heading_spans = [s for s in block.find_all("span") if _is_heading_span(s)]
            if heading_spans:
                if len(heading_spans) > 1:
                    self.fail_loud(
                        f"block on {page_url} contains {len(heading_spans)} distinct section headings "
                        f"({[s.get_text(' ', strip=True) for s in heading_spans]!r}) - expected at most 1 per block"
                    )
                flush()
                span = heading_spans[0]
                heading_text = span.get_text(" ", strip=True)
                if not heading_text:
                    self.fail_loud(f"a section-heading span on {page_url} has empty text")
                cur_heading = heading_text
                span.extract()

            text = block.get_text(" ", strip=True)
            links = [urljoin(page_url, a["href"]) for a in block.find_all("a", href=True)]
            if text:
                cur_text_parts.append(text)
            cur_links.extend(links)
        flush()

        if not sections:
            self.fail_loud(f"no content parsed from {_CONTENT_CONTAINER_SELECTOR!r} on {page_url}")

        return SphaProgramPage(
            page_title=page_title,
            page_url=page_url,
            sections=tuple(sections),
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                # No page attests one canonical effective/publication date -
                # see module docstring. Nullable per .claude/rules/data.md,
                # not a sentinel.
                published_date=None,
            ),
        )
