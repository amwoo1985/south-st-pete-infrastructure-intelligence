"""stpete.org DECISIONS #43 american_rescue_plan_act.php - the real target
of the ``/arpa`` vanity redirect DECISIONS #40 first surfaced. $45 million
in federal ARPA State and Local Fiscal Recovery Funds, broken into real
named sub-allocations, each carrying its own dollar amount and prose
description.

Scope is exactly this 1 URL DECISIONS #43 names - do not add another one
without a new DECISIONS.md entry, and do not follow any link found on this
page to a further page. This is explicitly a conscious widening of what
this source covers, not a natural extension of the grants/loans page tree
- see DECISIONS #43/module docstring for why it's still in scope.

Live recon (2026-08-20) confirmed this page does **not** share
``app/crawlers/stpete_program_details.py``'s single-container ``<h1>``/
``<h2>``/``<h3>`` template: ``#post .module-container`` matches 9
*separate* top-level content blocks on this page (an intro blurb, the
funding-allocation block, a projects-and-links nav block, an "In the
News" block, a reports-list block, an FAQ block, etc.), not one container
holding the whole page's content in document order. Reusing
``parse_program_detail_page()`` against this page would either silently
merge unrelated blocks into one section list or fail to find the funding
data at all - a different, targeted parser was built instead.

The real named funding sub-allocations live in exactly one of those 9
blocks, split across two side-by-side ``<div class="col-md-6">`` panels
each with its own ``<h2>`` heading whose text ends in "by the #s" -
"Housing by the #s" (6 allocations) and "Health & Social Equity by the
#s" (4 allocations), 10 total. Each allocation is one ``<p>`` beginning
with a ``<strong>$amount</strong>`` (plain dollar figure or "$N million"
shorthand - both forms observed live) followed by prose description text,
sometimes with one or more links (some to other stpete.org pages, some
off-host to project-specific domains - captured verbatim, never fetched,
same hard-stop discipline as every prior round). No other "by the #s"-style
heading was found anywhere else on the page (live recon checked every
``<h2>`` on the page, see DECISIONS #45).

Summing the 10 allocations' parsed dollar amounts gives ~$45.41 million,
consistent with (moderately over, matching the page's own "approximately
$45 million" phrasing) the page's headline $45 million figure - confirms
these 10 are the real, near-complete breakdown, not a partial or
mismatched extraction. DECISIONS #43's own recon excerpt named only 8 of
these 10 (it omitted the $1 million Permanent Supportive Housing
Wraparound Services and $405,000 Impact Monitoring allocations, both
confirmed live here) - see DECISIONS #45.

No clean per-allocation effective/publication date exists anywhere in this
block: the only date-shaped text found is a bare "through 2026" fragment
inside the $340,000 administrative-costs allocation's own prose, which is
too ambiguous to promote to a structured date (same "don't guess which
embedded date is the effective date" discipline as DECISIONS #37/#42).
Every allocation's ``published_date`` is ``None`` - nullable per
``.claude/rules/data.md``, not a sentinel. The page-level dates that do
exist (ARPA signed into federal law March 11, 2021; City's own funds
budgeted as of May 2021; must be allocated by December 31, 2024 and spent
by December 31, 2026) are general program-level facts in the FAQ block,
not attributable to any one named allocation, and are not modeled here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawlers.base import Attribution, BaseCrawler

# --- The 1 URL DECISIONS #43 names, exactly ---------------------------------
# The real target of the `/arpa` vanity redirect DECISIONS #40 found -
# fetched directly here, never via the redirect (see module docstring).

ARPA_URL = "https://www.stpete.org/government/initiatives___programs/american_rescue_plan_act.php"

# Every "#post .module-container" block whose own <h2> heading text ends
# in this marker carries named funding allocations - confirmed live
# (2026-08-20) to match exactly 2 headings ("Housing by the #s", "Health &
# Social Equity by the #s") and no others on the page. See module
# docstring / DECISIONS #45.
_ALLOCATION_CATEGORY_HEADING_SUFFIX = "by the #s"

_CONTENT_CONTAINER_SELECTOR = "#post .module-container"

# Matches "$6.5 million", "$160,000", "$946,435", "$1 million" - the 2
# dollar-amount shorthand forms observed live in this block's <strong>
# tags.
_AMOUNT_RE = re.compile(r"^\$([\d,]+(?:\.\d+)?)\s*(million)?$", re.IGNORECASE)


@dataclass(frozen=True)
class ArpaFundingAllocation:
    """One named ARPA sub-allocation - one <p> under a "... by the #s"
    <h2> heading. `amount_text` is the verbatim dollar figure as written
    on the page (e.g. "$6.5 million"); `amount_dollars` is that figure
    normalized to whole dollars for arithmetic/comparison (e.g. against
    the page's own $45 million headline total)."""

    category: str
    amount_text: str
    amount_dollars: int
    description: str
    links: tuple[str, ...]
    attribution: Attribution


class StpeteArpaCrawler(BaseCrawler):
    """Crawls DECISIONS #43's american_rescue_plan_act.php funding
    breakdown. See module docstring."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_arpa", **kwargs)

    def crawl(self) -> list[ArpaFundingAllocation]:
        resp = self.fetch(ARPA_URL)
        return self.parse_arpa_page(resp.text, ARPA_URL)

    def parse_arpa_page(self, html: str, page_url: str) -> list[ArpaFundingAllocation]:
        soup = BeautifulSoup(html, "lxml")

        base_tag = soup.find("base")
        link_base = base_tag.get("href") if base_tag is not None and base_tag.get("href") else page_url

        containers = soup.select(_CONTENT_CONTAINER_SELECTOR)
        if not containers:
            self.fail_loud(
                f"no {_CONTENT_CONTAINER_SELECTOR!r} content blocks found on {page_url} "
                "- stpete.org's ARPA page structure may have changed"
            )

        category_headings: list[Tag] = []
        for container in containers:
            for h2 in container.find_all("h2"):
                if h2.get_text(strip=True).endswith(_ALLOCATION_CATEGORY_HEADING_SUFFIX):
                    category_headings.append(h2)

        if not category_headings:
            self.fail_loud(
                f"no {_ALLOCATION_CATEGORY_HEADING_SUFFIX!r} category headings found on {page_url} "
                "- stpete.org's ARPA funding-breakdown structure may have changed"
            )

        retrieval_time = datetime.now(timezone.utc)

        allocations: list[ArpaFundingAllocation] = []
        for h2 in category_headings:
            category = h2.get_text(strip=True)
            panel = h2.parent
            if panel is None:
                self.fail_loud(f"category heading {category!r} on {page_url} has no parent element")

            paragraphs = panel.find_all("p", recursive=False)
            if not paragraphs:
                self.fail_loud(f"category {category!r} on {page_url} has no <p> allocation entries")

            for p in paragraphs:
                allocations.append(self._parse_allocation(p, category, page_url, link_base, retrieval_time))

        if not allocations:
            self.fail_loud(f"no funding allocations parsed from {page_url}")

        return allocations

    def _parse_allocation(
        self, p: Tag, category: str, page_url: str, link_base: str, retrieval_time: datetime
    ) -> ArpaFundingAllocation:
        strong = p.find("strong")
        if strong is None:
            self.fail_loud(f"an allocation <p> under category {category!r} on {page_url} has no <strong> amount")

        amount_text = strong.get_text(strip=True)
        amount_dollars = self._parse_amount_dollars(amount_text, category, page_url)

        full_text = p.get_text(" ", strip=True)
        if not full_text.startswith(amount_text):
            self.fail_loud(
                f"allocation <p> under category {category!r} on {page_url} does not start with its own "
                f"<strong> amount {amount_text!r} - unexpected markup"
            )
        description = full_text[len(amount_text) :].strip()
        if not description:
            self.fail_loud(f"allocation {amount_text!r} under category {category!r} on {page_url} has no description")

        links = tuple(dict.fromkeys(urljoin(link_base, a["href"]) for a in p.find_all("a", href=True)))

        return ArpaFundingAllocation(
            category=category,
            amount_text=amount_text,
            amount_dollars=amount_dollars,
            description=description,
            links=links,
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                # No clean per-allocation date exists on the page - see
                # module docstring. Nullable per .claude/rules/data.md,
                # not a guessed sentinel.
                published_date=None,
            ),
        )

    def _parse_amount_dollars(self, amount_text: str, category: str, page_url: str) -> int:
        match = _AMOUNT_RE.match(amount_text)
        if not match:
            self.fail_loud(
                f"could not parse dollar amount {amount_text!r} under category {category!r} on {page_url}"
            )
        number = float(match.group(1).replace(",", ""))
        if match.group(2):
            number *= 1_000_000
        return int(round(number))
