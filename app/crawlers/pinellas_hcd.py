"""Pinellas County Housing & Community Development (`pinellas.gov`),
DECISIONS #48/#52's 11 named pages:

    https://pinellas.gov/department/housing-and-community-development/
    https://pinellas.gov/programs/ayuda-pago-inicial/
    https://pinellas.gov/programs/community-development-neighborhood-stabilization-program/
    https://pinellas.gov/programs/florida-state-housing-initiatives-partnership-program/
    https://pinellas.gov/programs/home-investment-partnerships-program/
    https://pinellas.gov/programs/home-repair-loan-program/
    https://pinellas.gov/programs/independent-living-program/
    https://pinellas.gov/programs/lealman-commercial-improvement-grant-program/
    https://pinellas.gov/programs/lealman-residential-improvement-grant-program/
    https://pinellas.gov/programs/pinellas-county-hurricane-home-repair-program/
    https://pinellas.gov/programs/programas-de-prestamo-para-la-reparacion-de-viviendas-y-de-vida-independiente-revision-in-progress/

Scope is exactly these 11 URLs. Do not add another `pinellas.gov` page here
without a new DECISIONS.md entry first, and do not follow any link found on
these pages - same hard-stop discipline as every DECISIONS #27/#30/#32/#50/
#51 round before it. Two of the 11 (`ayuda-pago-inicial`,
`programas-de-prestamo-...`) are Spanish-language pages, built the same as
the other 9 - not skipped, not treated as second-class.

Live recon (2026-08-20) confirmed `pinellas.gov` is a real WordPress site
(Yoast `robots.txt` block, `wp-content`/Gutenberg block classes throughout)
- a completely different CMS from both `stpete.org` (custom PHP,
`#post .module-container`) and `www.stpeteha.org` (a different custom PHP
CMS, 36px-span headings). Two distinct page templates were confirmed live
among the 11:

**10 "program" pages** (everything except the department overview) share
one real template: a bare `<h1>` (no class) as the first child of
`#section-content`, followed by freeform Gutenberg block content - `<p>`
(`.wp-block-paragraph`), `<ul>`/`<ol>` (`.wp-block-list`), and `<table>`
(wrapped either in a `figure.wp-block-table` or a `div.wpDataTables`
plugin container - both confirmed server-rendered with real table rows,
not JS-only). Unlike every prior host in this crawl chain, **heading
levels here are not semantically consistent even within one page** -
`home-repair-loan-program` uses h3/h4/h5 and no h2 at all;
`pinellas-county-hurricane-home-repair-program` uses h2/h3/h4;
`lealman-residential-improvement-grant-program` uses h2/h3; a few pages
nest a heading *inside* a styled callout `<div>` (`div.block-callout`,
confirmed live on the hurricane page's "APPLICATIONS CLOSED" and "How to
apply" boxes) rather than as a sibling of it. Given no reliable per-level
meaning (unlike DECISIONS #35's stpete.org h2/h3 nesting, which was real),
every `<h2>`-`<h6>` found within `#section-content`, in document order, is
treated as one flat `PinellasSection` boundary - same "don't force a shape
that isn't cleanly and consistently there" call as DECISIONS #45/#50, just
applied to heading *level* instead of heading *meaning*. `PinellasSection`
keeps the tag name as `level` so a consumer can still see the raw
markup shape without this parser inventing a hierarchy that isn't there.

A stray, real `<a href>` not wrapped in a `<p>`/`<ul>`/`<ol>`/`<table>` -
e.g. the "Apply Now" button linking to the Neighborly Software application
portal (`div.wp-block-buttons > div.wp-block-button > a`, confirmed live on
the hurricane page) - is real, valuable content (the actual application
URL) that would otherwise be silently dropped by a parser that only reads
links out of matched block tags. These bare anchors are captured into the
current section's `links` (and their visible text, if any, into `text`)
the same way DECISIONS #50's CTA-button finding was handled, but kept
rather than excluded, since here there's no heading-span collision to guard
against - a bare anchor is never mistaken for a heading in this template.

Real dollar amounts and eligibility figures are present verbatim in section
text on multiple pages, confirmed live: `home-repair-loan-program` states
"Fully forgivable loan up to $75,000" and a separate "$20,000 grant" for
its Independent Living cross-reference, both **independent of** the
`pinellas.gov/news/...` article DECISIONS #48 flagged as unauthorized -
this is the "real dollar figure on an authorized page" DECISIONS #48 asked
this round to check for, and it's confirmed present.
`pinellas-county-hurricane-home-repair-program` states "Up to $30,000
grant" and full 120% AMI income tables by household size; that page's own
`div.block-callout` also states the program is now closed ("APPLICATIONS
CLOSED... reached capacity... New programs are expected to open this
fall"), captured verbatim, not silently dropped or treated as reason to
skip the page. `lealman-residential-improvement-grant-program` and
`lealman-commercial-improvement-grant-program` each carry their own real
funding caps ($15,000 / $100,000) and AMI-tiered match-percentage tables
(the latter server-rendered via a `wpDataTables` plugin table, not a plain
`<table>`, but structurally identical for parsing purposes).
`ayuda-pago-inicial` (Spanish) states real $50,000/$75,000 down-payment
assistance tiers by AMI band; `programas-de-prestamo-...` (Spanish) states
the same $75,000/$20,000 figures as the English `home-repair-loan-program`
page, confirming it's a real translation, not thin/placeholder content
despite its URL's "revision-in-progress" slug.
`community-development-neighborhood-stabilization-program` and
`florida-state-housing-initiatives-partnership-program` carry historical
NSP-round funding totals and SHIP program-purpose prose respectively, with
no dollar figure tied to a *current* open program on either page.
`home-investment-partnerships-program` and `independent-living-program`
state real AMI eligibility percentages and (for Independent Living) a real
$10,000 grant cap, but no application deadline.

**1 "department overview" page** (`department/housing-and-community-
development/`) is confirmed live to be a **structurally different
template** - not the `#section-content` h1/h2-h6 program-page shape at the
content level, though it reuses the same `#section-content`/`#section-
sidebar` two-column frame. Its real, page-owned content is a hero title
(`<h1 class="title">`, outside `#section-content`) plus a mission
`<div class="description">` paragraph, followed by a `<div id="quick-
container">` holding two `<h2>`-labelled card-decks ("Quick Facts": a real
"By 2045... grow by over 30,000 permanent residents" stat and a real "the
County has committed $23.4 million toward 1,197 units that include 884
affordable homes" stat; "Accomplishments": three narrative cards, two of
which link off pinellas.gov entirely - `homesforpinellas.org` and
`plan.pinellas.gov` - hosts not in `ALLOWED_SOURCE_HOSTS` and never
fetched here). These are modeled as `PinellasStatCard` - real, page-owned
prose, not a hub link.

Beyond `#quick-container`, the same `#section-content` region has 5 more
`<h2>` sections - "News & Stories", "Events", "Services", "Information &
Resources", "Programs" - and live recon confirmed **every one of these is
a hub**, not owned content: each links out to further `pinellas.gov` pages
none of which are named in DECISIONS #48/#52's 11-URL list. Concretely:
"News & Stories" links to 5 distinct `pinellas.gov/news/...` article pages,
including the exact
`pinellas.gov/news/pinellas-reopens-home-repair-program-offering-up-to-
75000-in-assistance/` URL DECISIONS #48 already named as *not* authorized
- confirmed reachable from this hub, still not fetched. "Events" links to
individual event pages. "Services" links to `/services/apply-for-*` permit/
application forms. "Information & Resources" links to NOFA/work-plan
documents. "Programs" links back to 7 of this round's own 10 program pages
(a subset - it does not list all 10, and lists none outside them). None of
these 5 sections' links are followed; each is captured as a
`PinellasHubSection` of `(title, url)` pairs only - same link-only,
never-fetched shape `StpeteGrantsCrawler.parse_tiles_page()` established
for hub tiles (DECISIONS #27/#32) - deliberately not the full
`PinellasSection` prose shape, so a hub link's mere presence can never be
mistaken for owned page content.

**Blocker for a future DECISIONS entry**, same hard-stop pattern as
DECISIONS #27/#30/#32/#50/#51: if any of the department page's "News &
Stories" article pages, "Events" pages, "Services" application-form pages,
"Information & Resources" documents, or the 2 off-host "Accomplishments"
links (`homesforpinellas.org`, `plan.pinellas.gov`) are wanted, they need
their own explicit URL naming and recon before any crawler follows them.

`published_date` stays `None` at the `Attribution` level for every page in
this module (nullable per `.claude/rules/data.md`) - no page attests one
canonical effective/publication date; real dates that do appear (the
hurricane program's "APPLICATIONS CLOSED"/"this fall" note, the department
page's dated news/event headlines captured only as hub-link titles) remain
in `text`/`title` verbatim, same discipline as DECISIONS #35/#45/#50.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler

# --- The 11 URLs DECISIONS #48/#52 name, exactly -----------------------------

DEPARTMENT_OVERVIEW_URL = "https://pinellas.gov/department/housing-and-community-development/"
AYUDA_PAGO_INICIAL_URL = "https://pinellas.gov/programs/ayuda-pago-inicial/"
COMMUNITY_DEVELOPMENT_NSP_URL = (
    "https://pinellas.gov/programs/community-development-neighborhood-stabilization-program/"
)
FLORIDA_SHIP_URL = "https://pinellas.gov/programs/florida-state-housing-initiatives-partnership-program/"
HOME_INVESTMENT_PARTNERSHIPS_URL = "https://pinellas.gov/programs/home-investment-partnerships-program/"
HOME_REPAIR_LOAN_PROGRAM_URL = "https://pinellas.gov/programs/home-repair-loan-program/"
INDEPENDENT_LIVING_PROGRAM_URL = "https://pinellas.gov/programs/independent-living-program/"
LEALMAN_COMMERCIAL_IMPROVEMENT_GRANT_URL = (
    "https://pinellas.gov/programs/lealman-commercial-improvement-grant-program/"
)
LEALMAN_RESIDENTIAL_IMPROVEMENT_GRANT_URL = (
    "https://pinellas.gov/programs/lealman-residential-improvement-grant-program/"
)
HURRICANE_HOME_REPAIR_PROGRAM_URL = "https://pinellas.gov/programs/pinellas-county-hurricane-home-repair-program/"
PRESTAMO_REPARACION_VIVIENDAS_ES_URL = (
    "https://pinellas.gov/programs/"
    "programas-de-prestamo-para-la-reparacion-de-viviendas-y-de-vida-independiente-revision-in-progress/"
)

# The 10 program pages - everything except the department overview, which
# is a structurally different template (see module docstring).
PROGRAM_PAGE_URLS: tuple[str, ...] = (
    AYUDA_PAGO_INICIAL_URL,
    COMMUNITY_DEVELOPMENT_NSP_URL,
    FLORIDA_SHIP_URL,
    HOME_INVESTMENT_PARTNERSHIPS_URL,
    HOME_REPAIR_LOAN_PROGRAM_URL,
    INDEPENDENT_LIVING_PROGRAM_URL,
    LEALMAN_COMMERCIAL_IMPROVEMENT_GRANT_URL,
    LEALMAN_RESIDENTIAL_IMPROVEMENT_GRANT_URL,
    HURRICANE_HOME_REPAIR_PROGRAM_URL,
    PRESTAMO_REPARACION_VIVIENDAS_ES_URL,
)

_CONTENT_CONTAINER_SELECTOR = "#section-content"

# Every heading level actually observed across the 10 program pages is not
# semantically consistent (see module docstring) - all are treated as flat
# section boundaries, in document order.
_HEADING_TAGS = ("h2", "h3", "h4", "h5", "h6")
_BLOCK_TAGS = ("p", "ul", "ol", "table")
_ALL_TAGS = (*_HEADING_TAGS, *_BLOCK_TAGS, "a")


def _is_top_level(el: Tag, container: Tag, exclude_ancestors: tuple[str, ...]) -> bool:
    """True unless `el` is nested inside another tag named in
    `exclude_ancestors`, up to `container`. Used both for block tags
    (p/ul/ol/table nested inside each other) and for bare `<a>` tags
    (excluded once they're already inside a matched p/ul/ol/table, whose
    own link-collection already captured them) - see module docstring's
    "Apply Now" button note."""
    for ancestor in el.parents:
        if ancestor is container:
            return True
        if getattr(ancestor, "name", None) in exclude_ancestors:
            return False
    return True


@dataclass(frozen=True)
class PinellasSection:
    """One heading-delimited block of content within a DECISIONS #48/#52
    program page's `#section-content`. `heading`/`level` are `None` for the
    lead content before the first real heading. `level` is the raw tag name
    (`"h2"`..`"h6"`) - kept as data, not turned into a nesting hierarchy,
    since heading levels aren't used consistently across these pages (see
    module docstring)."""

    heading: str | None
    level: str | None
    text: str | None
    links: tuple[str, ...]


@dataclass(frozen=True)
class PinellasProgramPage:
    """One of the 10 DECISIONS #48/#52 `pinellas.gov` program pages."""

    page_title: str
    page_url: str
    sections: tuple[PinellasSection, ...]
    attribution: Attribution


@dataclass(frozen=True)
class PinellasStatCard:
    """One "Quick Facts" / "Accomplishments" card on the department
    overview page - real, page-owned prose, not a hub link. `links` is
    rarely non-empty (e.g. an Accomplishments card's inline reference link)
    and is captured for transparency only, never fetched - some point off
    `pinellas.gov` entirely (see module docstring)."""

    heading: str
    text: str | None
    links: tuple[str, ...]


@dataclass(frozen=True)
class PinellasHubLink:
    """One link found in a department-overview hub-listing section (News &
    Stories / Events / Services / Information & Resources / Programs).
    Captured for transparency only - never fetched, same hard-stop
    discipline as every DECISIONS #27/#30/#32/#50/#51 round before it."""

    title: str
    url: str


@dataclass(frozen=True)
class PinellasHubSection:
    """One of the department overview page's 5 hub-listing sections. See
    module docstring - deliberately not a `PinellasSection` (no prose
    `text`), so a hub link can never be mistaken for owned page content."""

    heading: str
    links: tuple[PinellasHubLink, ...]


@dataclass(frozen=True)
class PinellasDepartmentPage:
    """The 1 DECISIONS #48/#52 department-overview page. See module
    docstring for why this is a structurally distinct template from the 10
    program pages."""

    page_title: str
    page_url: str
    description: str | None
    stat_cards: tuple[PinellasStatCard, ...]
    hub_sections: tuple[PinellasHubSection, ...]
    attribution: Attribution


class PinellasHcdCrawler(BaseCrawler):
    """Crawls the 11 DECISIONS #48/#52 `pinellas.gov` pages. See module
    docstring."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="pinellas_hcd", **kwargs)

    def crawl_program_pages(self) -> dict[str, PinellasProgramPage]:
        """Fetches all 10 program pages. Returns a dict keyed by page URL."""
        results: dict[str, PinellasProgramPage] = {}
        for url in PROGRAM_PAGE_URLS:
            resp = self.fetch(url)
            results[url] = self.parse_program_page(resp.text, url)
        return results

    def crawl_department_page(self) -> dict[str, PinellasDepartmentPage]:
        """Fetches the 1 department-overview page. Returns a dict keyed by
        page URL (single entry), matching the shape of
        `crawl_program_pages()`."""
        resp = self.fetch(DEPARTMENT_OVERVIEW_URL)
        return {DEPARTMENT_OVERVIEW_URL: self.parse_department_page(resp.text, DEPARTMENT_OVERVIEW_URL)}

    # --- Program pages (10) --------------------------------------------

    def parse_program_page(self, html: str, page_url: str) -> PinellasProgramPage:
        soup = BeautifulSoup(html, "lxml")

        container = soup.select_one(_CONTENT_CONTAINER_SELECTOR)
        if container is None:
            self.fail_loud(
                f"content container {_CONTENT_CONTAINER_SELECTOR!r} not found on {page_url} - "
                "pinellas.gov's page structure may have changed"
            )

        title_el = container.find("h1", recursive=False)
        if title_el is None:
            self.fail_loud(f"no <h1> page title found as a direct child of {_CONTENT_CONTAINER_SELECTOR!r} on {page_url}")
        page_title = title_el.get_text(strip=True)
        if not page_title:
            self.fail_loud(f"<h1> has empty text on {page_url}")

        retrieval_time = datetime.now(timezone.utc)

        sections: list[PinellasSection] = []
        cur_heading: str | None = None
        cur_level: str | None = None
        cur_text_parts: list[str] = []
        cur_links: list[str] = []

        def flush() -> None:
            nonlocal cur_heading, cur_level, cur_text_parts, cur_links
            text = " ".join(t for t in cur_text_parts if t).strip() or None
            if text or cur_links or cur_heading is not None:
                sections.append(
                    PinellasSection(
                        heading=cur_heading,
                        level=cur_level,
                        text=text,
                        links=tuple(dict.fromkeys(cur_links)),
                    )
                )
            cur_heading = None
            cur_level = None
            cur_text_parts = []
            cur_links = []

        for el in container.find_all(list(_ALL_TAGS), recursive=True):
            if el is title_el:
                continue

            if el.name in _HEADING_TAGS:
                flush()
                heading_text = el.get_text(strip=True)
                if not heading_text:
                    self.fail_loud(f"a {el.name} section heading on {page_url} has empty text")
                cur_heading = heading_text
                cur_level = el.name
                continue

            if el.name in _BLOCK_TAGS:
                if not _is_top_level(el, container, _BLOCK_TAGS):
                    continue
                text = el.get_text(" ", strip=True)
                links = [urljoin(page_url, a["href"]) for a in el.find_all("a", href=True)]
                if text:
                    cur_text_parts.append(text)
                cur_links.extend(links)
                continue

            # el.name == "a": a bare anchor not wrapped in p/ul/ol/table -
            # e.g. an "Apply Now" button (see module docstring). Skip if
            # it's inside an already-counted block (its href was already
            # collected above); otherwise it's real, standalone content.
            if not _is_top_level(el, container, _BLOCK_TAGS):
                continue
            href = el.get("href")
            if not href:
                continue
            link_text = el.get_text(" ", strip=True)
            if link_text:
                cur_text_parts.append(link_text)
            cur_links.append(urljoin(page_url, href))
        flush()

        if not sections:
            self.fail_loud(f"no content parsed from {_CONTENT_CONTAINER_SELECTOR!r} on {page_url}")

        return PinellasProgramPage(
            page_title=page_title,
            page_url=page_url,
            sections=tuple(sections),
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                # No page attests one canonical effective date - see module
                # docstring. Nullable per .claude/rules/data.md, not a
                # sentinel.
                published_date=None,
            ),
        )

    # --- Department overview page (1) ------------------------------------

    def parse_department_page(self, html: str, page_url: str) -> PinellasDepartmentPage:
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one("h1.title")
        if title_el is None:
            self.fail_loud(f"no 'h1.title' hero heading found on {page_url} - pinellas.gov's department-page structure may have changed")
        page_title = title_el.get_text(strip=True)
        if not page_title:
            self.fail_loud(f"'h1.title' has empty text on {page_url}")

        desc_el = soup.select_one("div.description")
        description = desc_el.get_text(" ", strip=True) if desc_el is not None else None
        description = description or None

        container = soup.select_one(_CONTENT_CONTAINER_SELECTOR)
        if container is None:
            self.fail_loud(
                f"content container {_CONTENT_CONTAINER_SELECTOR!r} not found on {page_url} - "
                "pinellas.gov's department-page structure may have changed"
            )

        retrieval_time = datetime.now(timezone.utc)

        stat_cards = self._parse_stat_cards(container, page_url)
        hub_sections = self._parse_hub_sections(container, page_url)

        return PinellasDepartmentPage(
            page_title=page_title,
            page_url=page_url,
            description=description,
            stat_cards=stat_cards,
            hub_sections=hub_sections,
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                # No page attests one canonical effective date - see module
                # docstring. Nullable per .claude/rules/data.md, not a
                # sentinel.
                published_date=None,
            ),
        )

    def _parse_stat_cards(self, container: Tag, page_url: str) -> tuple[PinellasStatCard, ...]:
        quick_container = container.select_one("#quick-container")
        if quick_container is None:
            self.fail_loud(
                f"'#quick-container' (Quick Facts/Accomplishments) not found within "
                f"{_CONTENT_CONTAINER_SELECTOR!r} on {page_url} - pinellas.gov's department-page structure may have changed"
            )

        cards = quick_container.select("div.card-deck > div.card")
        if not cards:
            self.fail_loud(f"'#quick-container' on {page_url} has no 'div.card-deck > div.card' stat cards")

        stat_cards: list[PinellasStatCard] = []
        for card in cards:
            strong = card.find("strong")
            if strong is None:
                self.fail_loud(f"a stat card in '#quick-container' on {page_url} has no <strong> heading")
            heading = strong.get_text(strip=True)
            if not heading:
                self.fail_loud(f"a stat card <strong> heading in '#quick-container' on {page_url} has empty text")
            paragraphs = card.find_all("p")
            text = " ".join(p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True)).strip() or None
            links = tuple(
                dict.fromkeys(urljoin(page_url, a["href"]) for a in card.find_all("a", href=True))
            )
            stat_cards.append(PinellasStatCard(heading=heading, text=text, links=links))

        return tuple(stat_cards)

    def _parse_hub_sections(self, container: Tag, page_url: str) -> tuple[PinellasHubSection, ...]:
        quick_container = container.select_one("#quick-container")
        all_h2 = container.find_all("h2")
        hub_h2s = [h2 for h2 in all_h2 if quick_container is None or quick_container not in h2.parents]
        if not hub_h2s:
            self.fail_loud(
                f"no hub-section <h2> headings found outside '#quick-container' within "
                f"{_CONTENT_CONTAINER_SELECTOR!r} on {page_url} - pinellas.gov's department-page structure may have changed"
            )

        hub_sections: list[PinellasHubSection] = []
        for h2 in hub_h2s:
            heading = h2.get_text(strip=True)
            if not heading:
                self.fail_loud(f"a hub-section <h2> on {page_url} has empty text")

            content = self._next_hub_content_sibling(h2)
            if content is None:
                self.fail_loud(
                    f"hub section {heading!r} on {page_url} has no following content container - "
                    "pinellas.gov's department-page structure may have changed"
                )

            anchors = [a for a in content.find_all("a", href=True) if a.get_text(strip=True)]
            if not anchors:
                self.fail_loud(f"hub section {heading!r} on {page_url} has no real (titled) links")

            links = tuple(
                dict.fromkeys(
                    PinellasHubLink(title=a.get_text(strip=True), url=urljoin(page_url, a["href"])) for a in anchors
                )
            )
            hub_sections.append(PinellasHubSection(heading=heading, links=links))

        return tuple(hub_sections)

    @staticmethod
    def _next_hub_content_sibling(h2: Tag) -> Tag | None:
        """The department page's hub-section headings sit in their own
        `div.d-flex...` wrapper, with the actual card-deck/listing content
        one or two real siblings later - a `<style>` tag (page-specific CSS
        for that section's listing) is often in between and must be
        skipped. See module docstring."""
        wrapper = h2.parent
        sib = wrapper.find_next_sibling()
        # Skip <style> tags (this template's per-section inline CSS) and any
        # non-Tag node (e.g. a whitespace-only NavigableString) - defensive
        # against parser whitespace handling, not just the <style> case
        # confirmed live.
        while sib is not None and (not isinstance(sib, Tag) or sib.name == "style"):
            sib = sib.find_next_sibling()
        return sib
