"""Chunking for every stpete.org page-shaped source: the grants/loans index
tiles (app/crawlers/stpete_grants.py), the category sub-pages (hub tiles,
the freeform for_south_stpete.php programs, and the Sunrise St. Pete tiles —
app/crawlers/stpete_grant_categories.py), and the h2-sectioned per-program
detail pages (app/crawlers/stpete_program_details.py).

Domain-aware boundaries per .claude/rules/rag.md:

- Tile items (StpeteGrantCategory, StpeteSunriseProgram) and freeform
  program blocks (StpeteGrantProgramDetail) are already one coherent,
  self-contained unit each — one item, one chunk, same as ARPA/Pinellas CF.
- H2-sectioned detail pages (StpeteProgramDetailPage) are chunked one
  <h2> section per chunk, not by fixed length — see DECISIONS #60 for the
  ambiguous-h2 judgment call (DECISIONS #37 already established that
  <h2> means "one program's section" on some pages and "a distinct
  program" on others, with no reliable way to tell from markup alone).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.stpete_grant_categories import StpeteGrantProgramDetail, StpeteSunriseProgram
from app.crawlers.stpete_grants import StpeteGrantCategory
from app.crawlers.stpete_program_details import StpeteProgramDetailPage

DOC_TYPE_GRANT_CATEGORY = "stpete_grant_category"
DOC_TYPE_PROGRAM_SUMMARY = "stpete_program_summary"
DOC_TYPE_SUNRISE_PROGRAM = "stpete_sunrise_program"
DOC_TYPE_PROGRAM_DETAIL = "stpete_program_detail"


# --- Index / hub-page tiles (StpeteGrantCategory) ---------------------------


def chunk_stpete_grant_category(category: StpeteGrantCategory) -> Chunk:
    """One tile from the index page or one of the 4 pure-hub pages
    (DECISIONS #27/#32) — name + link, optionally a caption. Every tile
    observed live has an empty caption (description=None), but the
    category name alone is still a legitimate, if thin, navigational
    chunk: dropping it would leave that part of the site's category
    structure completely unindexed."""
    text = category.category_name
    if category.description:
        text = f"{text}: {category.description}"

    source_url = category.attribution.source_url
    chunk_id = make_chunk_id(source_url, "grant_category", category.category_url)

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_GRANT_CATEGORY,
        text=text,
        section_label=category.category_name,
        attribution=category.attribution,
    )


def chunk_stpete_grant_categories(categories: Iterable[StpeteGrantCategory]) -> list[Chunk]:
    return [chunk_stpete_grant_category(c) for c in categories]


# --- for_south_stpete.php freeform program blocks (StpeteGrantProgramDetail) -


def chunk_stpete_program_summary(program: StpeteGrantProgramDetail) -> Chunk:
    """One <h3>/<h4> program block from for_south_stpete.php (DECISIONS
    #33) — already one coherent, self-contained unit, same reasoning as
    the ARPA/Pinellas CF row-based sources."""
    text = program.program_name
    if program.description:
        text = f"{text}. {program.description}"

    source_url = program.attribution.source_url
    chunk_id = make_chunk_id(source_url, "program_summary", program.program_name)

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_PROGRAM_SUMMARY,
        text=text,
        section_label=program.program_name,
        attribution=program.attribution,
    )


def chunk_stpete_program_summaries(programs: Iterable[StpeteGrantProgramDetail]) -> list[Chunk]:
    return [chunk_stpete_program_summary(p) for p in programs]


# --- sunrise_st._pete/index.php tiles (StpeteSunriseProgram) ----------------


def chunk_stpete_sunrise_program(program: StpeteSunriseProgram) -> Chunk:
    """One "Active Programs" tile from the Sunrise St. Pete page
    (DECISIONS #34) — real per-program eligibility + description embedded
    directly in the tile, already one coherent unit."""
    parts = [program.program_name]
    if program.eligibility:
        parts.append(f"Eligibility: {program.eligibility}")
    if program.description:
        parts.append(program.description)
    text = " ".join(parts)

    source_url = program.attribution.source_url
    chunk_id = make_chunk_id(source_url, "sunrise_program", program.program_url)

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_SUNRISE_PROGRAM,
        text=text,
        section_label=program.program_name,
        attribution=program.attribution,
    )


def chunk_stpete_sunrise_programs(programs: Iterable[StpeteSunriseProgram]) -> list[Chunk]:
    return [chunk_stpete_sunrise_program(p) for p in programs]


# --- H2-sectioned per-program detail pages (StpeteProgramDetailPage) --------


def chunk_stpete_program_detail_page(page: StpeteProgramDetailPage) -> list[Chunk]:
    """One chunk per <h2> section. See DECISIONS #60: DECISIONS #37 found
    that <h2> means "one program's section" (Overview/Eligibility/How To
    Apply) on most of these pages but "a distinct named program" on a few
    (for_developers.php, for_property_owners.php, solar.php,
    stormwater_utility_fee_credits.php, tax_incentives.php) — with no
    reliable structural signal to tell the two cases apart. Rather than
    guess, every <h2> becomes its own chunk regardless of which case it
    is: a query about one specific program's deadline shouldn't need
    every other program on a 13-program page in the same chunk, and on a
    genuinely single-program page an extra "Eligibility"/"Documents"-level
    chunk boundary is a normal, well-scoped chunk, not an error.

    Each chunk carries the page_title (and the page's intro paragraph, if
    any) as a context prefix, and folds in every <h3>/<h4> subsection
    nested under that <h2> (StpeteProgramSection.text is only the
    direct-under-h2 paragraph content; subsection text lives separately in
    StpeteProgramSection.subsections and must be included explicitly) — so
    a chunk pulled out of a 13-program page still names which page it came
    from and carries its own subsections' content, rather than reading as
    an orphaned fragment."""
    chunks: list[Chunk] = []
    heading_occurrences: dict[str, int] = {}

    for section in page.sections:
        parts = [f"{page.page_title}."]
        if page.intro:
            parts.append(page.intro)
        parts.append(f"{section.heading}.")
        if section.text:
            parts.append(section.text)
        for subsection in section.subsections:
            parts.append(f"{subsection.heading}:")
            if subsection.text:
                parts.append(subsection.text)
        text = " ".join(parts)

        # Defensive disambiguation for the (unobserved live, but not
        # structurally impossible) case of two <h2>s sharing heading text
        # on the same page — deterministic given document order, so still
        # idempotent across re-runs.
        occurrence = heading_occurrences.get(section.heading, 0)
        heading_occurrences[section.heading] = occurrence + 1
        identity_key = section.heading if occurrence == 0 else f"{section.heading}#{occurrence}"

        chunk_id = make_chunk_id(page.page_url, "program_detail_section", identity_key)
        section_label = f"{page.page_title} — {section.heading}"

        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_PROGRAM_DETAIL,
                text=text,
                section_label=section_label,
                attribution=page.attribution,
            )
        )

    return chunks


def chunk_stpete_program_detail_pages(pages: Iterable[StpeteProgramDetailPage]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        chunks.extend(chunk_stpete_program_detail_page(page))
    return chunks
