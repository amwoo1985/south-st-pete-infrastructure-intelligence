"""Chunking for St. Petersburg Housing Authority (www.stpeteha.org)
sources: the 8 DECISIONS #48/#50 program-info pages
(app/crawlers/spha_program_pages.py's SphaProgramPage) and the DECISIONS
#48/#51 /news index (app/crawlers/spha_news.py's SphaNewsIndexItem).

Per-section boundary call for SphaProgramPage - compared against, not
copied from, DECISIONS #60's stpete.org h2-ambiguity reasoning: DECISIONS
#60's per-<h2> answer exists because DECISIONS #37 found <h2> means two
different things on different stpete.org pages ("one program's
subsection" vs "a distinct program") with no reliable way to tell which,
so it chunked on the one reliable signal it had (the heading boundary
itself) purely to avoid guessing wrong. SPHA's shape doesn't carry that
same ambiguity - live recon (app/crawlers/spha_program_pages.py's module
docstring, confirmed again here against the real 8 fixtures) shows every
page is about exactly one program/topic, with sections that are FAQ-style
subtopics of that one thing ("Paying Rent", "Eligibility", "Renting a
Unit" all under public-housing-clients). There's no "is this heading a
subsection or a whole new program?" question to beg here. The per-section
chunking call instead rests on ordinary query-scoping: "how do I pay
rent" and "what's the eligibility" are different, specific questions, and
keeping "Paying Rent" and "Eligibility" separately retrievable serves
precision - a citation for a rent question shouldn't need to drag in the
whole page's FAQ, Recertification, and Scholarship Program sections too.

Two shapes observed live, both handled the same way (one chunk per
section) rather than as special cases:
- The `heading=None` lead section (content before the first real heading)
  gets its own chunk like any other section - it's real, substantial
  intro prose on every page that has one (e.g. public-housing-clients'
  "Established by the federal gov[ernment]..." opening), not a throwaway
  fragment. Its section_label reads "<page_title> — Introduction" since
  there's no heading text to use. (Confirmed live: a page can have at most
  one heading=None section - it can only be the lead content before the
  first real heading, per SphaProgramPagesCrawler's flush()/parse loop -
  so "Introduction" never collides with a second lead section on the same
  page.)
- `performance-report` has zero 36px headings at all (confirmed live), so
  its single section is heading=None covering the whole page - this
  produces exactly one chunk, the same "one section, one chunk" rule
  applied uniformly, not a hand-carved special case.

SphaNewsIndexItem: one row = one chunk, same DECISIONS #62 row-based
reasoning as Pinellas CF/ARPA - and unlike stpete_commitment's action
items (see app/chunking/stpete_commitment.py's module docstring), this
source DOES have a good non-text-content identity field: `item_url` (e.g.
"/news-view?id=688"), confirmed unique across all 20 real items on the
fixture. It's a structural key (this CMS's own article ID), not the
chunk's own prose - using it doesn't run into the tension
stpete_commitment.py had to flag and accept anyway. Because the CMS
truncates `title` server-side (real, baked into the HTML, ending in a
literal "..." - not a CSS visual effect), the chunk text says so
explicitly and points at `item_url` for the full article rather than
pretending to have content that was never fetched - same "cite a
hub/summary-only source honestly" discipline as DECISIONS #60's thin
category tiles.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.spha_news import SphaNewsIndexItem
from app.crawlers.spha_program_pages import SphaProgramPage

DOC_TYPE_SPHA_PROGRAM_SECTION = "spha_program_section"
DOC_TYPE_SPHA_NEWS_ITEM = "spha_news_index_item"

_LEAD_IDENTITY_KEY = "lead"
_LEAD_LABEL = "Introduction"


# --- 8 program-info pages (SphaProgramSection) -------------------------------


def chunk_spha_program_page(page: SphaProgramPage) -> list[Chunk]:
    """One chunk per SphaProgramSection, in document order - see module
    docstring for why this is a per-section, not per-page, boundary here."""
    chunks: list[Chunk] = []
    heading_occurrences: dict[str, int] = {}

    for section in page.sections:
        heading_label = section.heading if section.heading is not None else _LEAD_LABEL

        parts = [f"{page.page_title}.", f"{heading_label}."]
        if section.text:
            parts.append(section.text)
        text = " ".join(parts)

        if section.heading is None:
            # Only the lead section (content before the first real
            # heading) can have heading=None - see module docstring - so
            # this identity key is safe without an occurrence counter.
            identity_key = _LEAD_IDENTITY_KEY
        else:
            # Defensive disambiguation for the (unobserved live, but not
            # structurally impossible) case of two sections sharing
            # heading text on one page - same discipline as
            # app/chunking/stpete_pages.py's h2-sectioned pages.
            occurrence = heading_occurrences.get(section.heading, 0)
            heading_occurrences[section.heading] = occurrence + 1
            identity_key = section.heading if occurrence == 0 else f"{section.heading}#{occurrence}"

        chunk_id = make_chunk_id(page.page_url, "spha_program_section", identity_key)
        section_label = f"{page.page_title} — {heading_label}"

        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_SPHA_PROGRAM_SECTION,
                text=text,
                section_label=section_label,
                attribution=page.attribution,
            )
        )

    return chunks


def chunk_spha_program_pages(pages: Iterable[SphaProgramPage]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        chunks.extend(chunk_spha_program_page(page))
    return chunks


# --- /news index (SphaNewsIndexItem) -----------------------------------------


def chunk_spha_news_item(item: SphaNewsIndexItem) -> Chunk:
    """One /news index headline. `item_url` (a real per-item CMS article
    ID, not the chunk's own text content) is the identity field - see
    module docstring. The chunk text names the CMS's server-side title
    truncation explicitly and points at `item_url`, rather than presenting
    a truncated fragment as if it were the whole story."""
    text = (
        f"SPHA news item ({item.published_date.isoformat()}): {item.title} "
        f"[title truncated by stpeteha.org's CMS; full article not fetched, see {item.item_url}]"
    )

    chunk_id = make_chunk_id(item.attribution.source_url, "spha_news_item", item.item_url)
    section_label = f"SPHA News — {item.published_date.isoformat()}"

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_SPHA_NEWS_ITEM,
        text=text,
        section_label=section_label,
        attribution=item.attribution,
    )


def chunk_spha_news_items(items: Iterable[SphaNewsIndexItem]) -> list[Chunk]:
    return [chunk_spha_news_item(i) for i in items]
