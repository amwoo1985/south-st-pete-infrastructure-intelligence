"""Chunking for Pinellas County Housing & Community Development
(pinellas.gov), DECISIONS #48/#52/#53's 11 pages:
app/crawlers/pinellas_hcd.py's PinellasProgramPage (10 program pages) and
PinellasDepartmentPage (1 department-overview page).

PinellasSection boundary call (10 program pages) - the same
per-section-vs-whole-page judgment SPHA (app/chunking/spha.py) and
stpete.org's h2-sectioned pages (app/chunking/stpete_pages.py, DECISIONS
#60) both needed, reasoned fresh for this specific shape rather than
inherited from either: unlike DECISIONS #60's stpete.org ambiguity (does
<h2> mean subsection or a distinct program?) and closer to SPHA's cleaner
case, each pinellas.gov program page covers exactly one program -
"home-repair-loan-program" is one page about the Home Repair Loan, its
"How to Apply"/"Eligible Improvements" headings are subsections of that
one program, not disguised sibling programs. This source's real
complication is different in kind from both prior cases, though: heading
*level* is not semantically consistent (one page uses h3/h4/h5 with no h2
at all; another uses h2/h3/h4; headings sometimes nest inside a styled
callout <div> rather than sit as its sibling - see
app/crawlers/pinellas_hcd.py's module docstring). The crawler already
made the call to flatten every h2-h6 into one non-hierarchical
PinellasSection sequence rather than invent a nesting structure that
isn't reliably there. Chunking follows that same flattening: one chunk
per PinellasSection, in document order, regardless of its raw tag level -
justified by SPHA's query-scoping reasoning (a specific "how do I apply"
question shouldn't need the whole page in one chunk), not a
re-litigation of the crawler's own flattening call.

PinellasStatCard / department-page split (1 department-overview page):
- Each PinellasStatCard is real, page-owned prose - same row-based
  reasoning as DECISIONS #62, one card = one chunk, identified by its own
  heading. Live recon against the real department.html fixture found 6
  real cards, not the 2 ("Quick Facts"'s pair) the DECISIONS #48/#52 recon
  quote focused on - "Our Mission" and 3 "Accomplishments" cards are also
  real, page-owned prose on the actual page, and all 6 get their own
  chunk here (heading text confirmed unique across all 6 on the live
  fixture, used as the row identity, same as DECISIONS #62's
  program_name).
- `page_title` + `description` (the hero mission paragraph) is its own
  substantial block of real prose, independent of any one stat card - it
  gets its own chunk rather than being folded as a prefix into all 6 stat
  cards (which would duplicate it six times over - a context-stuffing
  risk in the opposite direction from under-fragmentation) or dropped
  entirely (which would lose real content the page actually states).

PinellasHubSection / PinellasHubLink: deliberately NOT chunked - no
chunker is written for them at all, a considered exclusion, not an
oversight. All 5 hub sections (News & Stories, Events, Services,
Information & Resources, Programs) are, by the crawler's own dataclass
shape and its module docstring, link-only with no owned-prose field at
all (no `text` on PinellasHubSection) - every one links out to pages
nobody has fetched (5 pinellas.gov/news articles, event pages,
application forms, NOFA documents, and 2 off-host "Accomplishments"-style
links). Chunking them would mean producing a "chunk" that's just a
heading plus a handful of link titles pointing at unread pages -
something that would look citable in a retrieval hit but represents no
page-owned knowledge at all, misrepresenting what this system actually
knows. Leaving them out is the honest choice here, matching DECISIONS
#27/#30/#32/#50/#51's same "don't follow, don't pretend to have"
discipline the crawler itself already applied by never fetching them.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.pinellas_hcd import PinellasDepartmentPage, PinellasProgramPage

DOC_TYPE_PINELLAS_PROGRAM_SECTION = "pinellas_hcd_program_section"
DOC_TYPE_PINELLAS_DEPARTMENT_DESCRIPTION = "pinellas_hcd_department_description"
DOC_TYPE_PINELLAS_STAT_CARD = "pinellas_hcd_stat_card"

_LEAD_IDENTITY_KEY = "lead"
_LEAD_LABEL = "Introduction"


# --- 10 program pages (PinellasSection) --------------------------------------


def chunk_pinellas_program_page(page: PinellasProgramPage) -> list[Chunk]:
    """One chunk per PinellasSection, in document order, regardless of raw
    heading level (h2-h6, kept flat, non-hierarchical - see module
    docstring).

    Contentless-heading forward merge (DECISIONS #67, rag-review finding):
    confirmed live on 4 of the 10 real fixtures (home-repair-loan-program,
    both lealman pages, and the Spanish revision-in-progress page) that
    PinellasSection's flat, level-agnostic model (DECISIONS #53) sometimes
    puts a real parent heading (e.g. an h3 "Eligible Improvements") with no
    content of its own immediately before its real child content, which the
    flat model records as sibling sections rather than nested ones. Chunking
    that heading on its own would produce a chunk whose only text is the
    page title plus the heading itself repeated - non-empty (passes
    Chunk.__post_init__) but substantively empty, and it would score highly
    on embedding similarity for exactly the query it can't answer (e.g.
    "what improvements are eligible") while carrying none of the real
    answer. A contentless heading is folded forward into the SINGLE next
    section that does carry content, as a compound label - not into every
    subsequent section at a deeper heading level, which was considered and
    rejected: home-repair-loan-program's "Eligible Improvements" (h3) is
    followed by 2 real h4 children *and then 2 more h4 siblings unrelated to
    it* ("Required Documents", "Application details and timeline") at the
    same nominal depth - a level-based merge would have falsely folded those
    two unrelated sections under "Eligible Improvements" too, fabricating a
    hierarchy relationship the source page doesn't actually have (exactly
    what DECISIONS #53 already declined to do at the crawler layer, for the
    same reason: heading levels aren't semantically consistent here). The
    accepted tradeoff, stated plainly: a true second/third child section
    immediately following the first (e.g. Independent Living Program, right
    after Home Repair Loan Program folds in "Eligible Improvements") does
    NOT also get the parent heading's label - only the first-following
    section does, matching the single-hop forward-merge precedent DECISIONS
    #59 already used for Legistar's stub agenda items. If a page ends with
    an unmerged contentless heading (not observed live, but not ruled out),
    it still gets emitted as its own thin, heading-only chunk - dropping it
    silently would erase a real (if content-free) part of the page's
    structure, same "a bare label chunk is still legitimate, if thin"
    precedent as DECISIONS #60's category tiles."""
    chunks: list[Chunk] = []
    heading_occurrences: dict[str, int] = {}
    pending_headings: list[str] = []

    def flush_pending_as_thin_chunk() -> None:
        """Emits any unmerged trailing contentless headings as their own
        thin chunk (page ended before real content followed them) rather
        than silently dropping them. See function docstring."""
        if not pending_headings:
            return
        compound_label = " › ".join(pending_headings)
        _append_chunk(chunks, heading_occurrences, page, compound_label, text_parts=None)
        pending_headings.clear()

    for section in page.sections:
        heading_label = section.heading if section.heading is not None else _LEAD_LABEL
        has_content = bool(section.text) or bool(section.links)

        if section.heading is not None and not has_content:
            pending_headings.append(heading_label)
            continue

        compound_label = " › ".join([*pending_headings, heading_label]) if pending_headings else heading_label
        pending_headings = []

        _append_chunk(
            chunks,
            heading_occurrences,
            page,
            compound_label,
            text_parts=[section.text] if section.text else None,
            is_lead=section.heading is None,
        )

    flush_pending_as_thin_chunk()

    return chunks


def _append_chunk(
    chunks: list[Chunk],
    heading_occurrences: dict[str, int],
    page: PinellasProgramPage,
    compound_label: str,
    text_parts: list[str] | None,
    is_lead: bool = False,
) -> None:
    parts = [f"{page.page_title}.", f"{compound_label}."]
    if text_parts:
        parts.extend(t for t in text_parts if t)
    text = " ".join(parts)

    if is_lead:
        # Only the lead content before the first real heading can produce
        # this - confirmed live, at most one per page - so this identity
        # key is safe without an occurrence counter.
        identity_key = _LEAD_IDENTITY_KEY
    else:
        # Defensive disambiguation for the (unobserved live, but not
        # structurally impossible) case of two sections sharing the same
        # compound label on one page - same discipline as
        # app/chunking/stpete_pages.py's h2-sectioned pages and
        # app/chunking/spha.py.
        occurrence = heading_occurrences.get(compound_label, 0)
        heading_occurrences[compound_label] = occurrence + 1
        identity_key = compound_label if occurrence == 0 else f"{compound_label}#{occurrence}"

    chunk_id = make_chunk_id(page.page_url, "pinellas_program_section", identity_key)
    section_label = f"{page.page_title} — {compound_label}"

    chunks.append(
        Chunk(
            chunk_id=chunk_id,
            doc_type=DOC_TYPE_PINELLAS_PROGRAM_SECTION,
            text=text,
            section_label=section_label,
            attribution=page.attribution,
        )
    )


def chunk_pinellas_program_pages(pages: Iterable[PinellasProgramPage]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        chunks.extend(chunk_pinellas_program_page(page))
    return chunks


# --- 1 department-overview page (PinellasDepartmentPage) --------------------


def chunk_pinellas_department_page(page: PinellasDepartmentPage) -> list[Chunk]:
    """One chunk for the page's own description (hero mission paragraph),
    if present, plus one chunk per PinellasStatCard. Deliberately produces
    NO chunk at all for page.hub_sections - see module docstring."""
    chunks: list[Chunk] = []

    if page.description:
        text = f"{page.page_title}. {page.description}"
        chunk_id = make_chunk_id(page.page_url, "pinellas_department_description")
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_PINELLAS_DEPARTMENT_DESCRIPTION,
                text=text,
                section_label=page.page_title,
                attribution=page.attribution,
            )
        )

    card_heading_occurrences: dict[str, int] = {}
    for card in page.stat_cards:
        text = f"{page.page_title} — {card.heading}."
        if card.text:
            text = f"{text} {card.text}"

        # Same defensive disambiguation as chunk_pinellas_program_page's
        # heading_occurrences (rag-review MED finding, DECISIONS #67) - all
        # 6 real cards on the live fixture have distinct headings, but
        # nothing guarantees that stays true, and an undetected collision
        # here would silently overwrite one card's chunk on upsert.
        occurrence = card_heading_occurrences.get(card.heading, 0)
        card_heading_occurrences[card.heading] = occurrence + 1
        identity_key = card.heading if occurrence == 0 else f"{card.heading}#{occurrence}"

        chunk_id = make_chunk_id(page.page_url, "pinellas_stat_card", identity_key)
        section_label = f"{page.page_title} — {card.heading}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_PINELLAS_STAT_CARD,
                text=text,
                section_label=section_label,
                attribution=page.attribution,
            )
        )

    return chunks


def chunk_pinellas_department_pages(pages: Iterable[PinellasDepartmentPage]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        chunks.extend(chunk_pinellas_department_page(page))
    return chunks
