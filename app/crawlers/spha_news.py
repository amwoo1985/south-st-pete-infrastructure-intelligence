"""St. Petersburg Housing Authority (`www.stpeteha.org`) news index,
DECISIONS #48's 9th named page:

    https://www.stpeteha.org/news

Scope is exactly this 1 URL - no query params (no `?year=`/`?category=`
filter, no `?grp=` pagination page beyond the first). Do not follow any
`/news-view?id=...` link found on this page - DECISIONS #48 explicitly
does not authorize the individual news-item pages, and this module's own
recon (2026-08-20) confirms exactly why that's the right call: see below.

Live recon found `/news` shares none of `app/crawlers/spha_program_pages.
py`'s `#bodyContainer` template - it's a distinct, index-only shape. The
page title is `<h2 class="ptitles">Latest News</h2>` (the 8 program pages
use `<h1 class="ptitles">` - a CMS-level inconsistency, not modeled, since
the title text itself is what matters here). The real content is
`#cms-body-content div.press-items`, a flat list of 20 items (this CMS's
default page size - the paging bar shows 5 total pages, `?grp=40/60/80/
100`, none of which are fetched, per scope), each exactly:

    <div class="press-items"><a href="/news-view?id=688">
        <strong>08/20/2026</strong> - Public Notice - Regular Meeting of
        the St. Petersburg Housing Authorit...
    </a></div>

Every one of the 20 items observed live carries a real, structured
`MM/DD/YYYY` date (this CMS's own convention, a `<strong>`-wrapped date
prefix, not freeform prose) - the first page in this whole crawl chain
where a genuine per-item `published_date` exists, parsed here rather than
left `None`. But every item's title is **itself already truncated by the
CMS**, ending in a literal `...` in the raw HTML (confirmed: not a CSS
`text-overflow: ellipsis` visual effect - the truncation is server-side,
baked into the HTML this page's own markup sends). This means the index
page carries no full-length title, and no body content, dollar amount, or
date beyond the one per-item summary date - the same "hub with only a
name and a link" shape DECISIONS #27/#32 found on stpete.org's grants
category pages, not a page with real per-item content of its own.

DECISIONS #48's own recon quoted a live $842K HUD Capital Fund award and a
$104K Family Self-Sufficiency grant as "reported on its news pages" - live
recon during this session searched all 9 DECISIONS #48 pages (this index
and the 8 `spha_program_pages.py` pages) for any dollar amount and found
**none** on any of them. Those two figures almost certainly live in the
body of one or more individual `news-view?id=...` articles - exactly the
sub-pages this entry does not authorize following. **Blocker for a future
DECISIONS entry:** if those two dollar figures (or any other individual
news-item content) are wanted, the specific `news-view?id=...` URL(s) need
to be identified and named explicitly, same discipline as every prior
DECISIONS #27/#32/#35 hub-page-to-detail-page expansion.

`SphaNewsIndexItem.attribution.source_url` is always `NEWS_URL` (the index
page actually fetched); `item_url` captures the individual article link
verbatim, never fetched, same hard-stop discipline as
`StpeteProgramSection.links` (DECISIONS #37) and every prior round.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.crawlers.base import Attribution, BaseCrawler

# --- The 1 URL DECISIONS #48 names for the news index, exactly -------------

NEWS_URL = "https://www.stpeteha.org/news"

_TITLE_SELECTOR = ".ptitles"
_ITEMS_CONTAINER_SELECTOR = "#cms-body-content"
_ITEM_SELECTOR = "div.press-items"

_DATE_FORMAT = "%m/%d/%Y"


@dataclass(frozen=True)
class SphaNewsIndexItem:
    """One dated headline on the `/news` index. `title` is the CMS's own
    truncated summary text (see module docstring - the full article lives
    at `item_url`, not fetched). `published_date` is a real, structured
    per-item date - not nullable here, unlike every other DECISIONS #48
    page, because this CMS's own `<strong>` date-prefix convention is a
    real structural signal, not freeform prose."""

    published_date: date
    title: str
    item_url: str
    attribution: Attribution


class SphaNewsCrawler(BaseCrawler):
    """Crawls DECISIONS #48's `/news` index. See module docstring - this
    does not fetch any `news-view?id=...` article page."""

    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="spha_news", **kwargs)

    def crawl(self) -> list[SphaNewsIndexItem]:
        resp = self.fetch(NEWS_URL)
        return self.parse_news_index(resp.text, NEWS_URL)

    def parse_news_index(self, html: str, page_url: str) -> list[SphaNewsIndexItem]:
        soup = BeautifulSoup(html, "lxml")

        title_el = soup.select_one(_TITLE_SELECTOR)
        if title_el is None or not title_el.get_text(strip=True):
            self.fail_loud(
                f"no {_TITLE_SELECTOR!r} page title found on {page_url} - "
                "stpeteha.org's news-index markup may have changed"
            )

        container = soup.select_one(_ITEMS_CONTAINER_SELECTOR)
        if container is None:
            self.fail_loud(
                f"items container {_ITEMS_CONTAINER_SELECTOR!r} not found on {page_url} - "
                "stpeteha.org's news-index page structure may have changed"
            )

        item_divs = container.select(_ITEM_SELECTOR)
        if not item_divs:
            self.fail_loud(f"no {_ITEM_SELECTOR!r} news items found in {_ITEMS_CONTAINER_SELECTOR!r} on {page_url}")

        retrieval_time = datetime.now(timezone.utc)

        items: list[SphaNewsIndexItem] = []
        for div in item_divs:
            items.append(self._parse_item(div, page_url, retrieval_time))
        return items

    def _parse_item(self, div, page_url: str, retrieval_time: datetime) -> SphaNewsIndexItem:
        a = div.find("a", href=True)
        if a is None:
            self.fail_loud(f"a {_ITEM_SELECTOR!r} block on {page_url} has no link to its article")

        strong = a.find("strong")
        if strong is None:
            self.fail_loud(f"a {_ITEM_SELECTOR!r} block on {page_url} has no <strong> date prefix")
        date_text = strong.get_text(strip=True)
        try:
            published_date = datetime.strptime(date_text, _DATE_FORMAT).date()
        except ValueError:
            self.fail_loud(
                f"news item date {date_text!r} on {page_url} doesn't match expected {_DATE_FORMAT!r} format"
            )

        full_text = a.get_text(" ", strip=True)
        title = full_text[len(date_text) :].strip()
        title = title.lstrip("-").strip()
        if not title:
            self.fail_loud(f"a news item on {page_url} (dated {date_text}) has no title text")

        item_url = urljoin(page_url, a["href"])

        return SphaNewsIndexItem(
            published_date=published_date,
            title=title,
            item_url=item_url,
            attribution=Attribution(
                source_url=page_url,
                retrieval_timestamp=retrieval_time,
                published_date=published_date,
            ),
        )
