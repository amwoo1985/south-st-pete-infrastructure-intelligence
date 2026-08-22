"""Chunking for the two "already one row = one coherent unit" sources:
Pinellas Community Foundation grant programs
(app/crawlers/pinellas_cf.py's PinellasCFGrantProgram) and stpete.org's
ARPA funding allocations (app/crawlers/stpete_arpa.py's
ArpaFundingAllocation).

Unlike the AMI table (app/chunking/ami_table.py, DECISIONS #61) or the
h2-sectioned stpete.org pages (app/chunking/stpete_pages.py, DECISIONS
#48), neither source needs a grouping/splitting judgment call — each row
is already exactly the unit a citation should point at: one named grant
program with its own timeline, or one named funding allocation with its
own dollar amount and description. One row, one chunk. See DECISIONS #62.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.pinellas_cf import PinellasCFGrantProgram
from app.crawlers.stpete_arpa import ArpaFundingAllocation

DOC_TYPE_PINELLAS_CF_GRANT = "pinellas_cf_grant"
DOC_TYPE_ARPA_ALLOCATION = "arpa_funding_allocation"


# --- Pinellas Community Foundation grant programs ---------------------------


def chunk_pinellas_cf_program(program: PinellasCFGrantProgram) -> Chunk:
    text = (
        f"{program.program_name}. Application timeline: {program.application_timeline}. "
        f"Award distribution: {program.award_distribution}."
    )

    source_url = program.attribution.source_url
    # program_name is unique per row in the real data even where 3 rows
    # share one underlying program (e.g. "Senior Citizens Services Grants:
    # Housing"/"...: Wellness"/"...: Support" are 3 distinct funding-cycle
    # rows with 3 distinct names) - confirmed live, see DECISIONS #62.
    chunk_id = make_chunk_id(source_url, "pinellas_cf_grant", program.program_name)

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_PINELLAS_CF_GRANT,
        text=text,
        section_label=program.program_name,
        attribution=program.attribution,
    )


def chunk_pinellas_cf_programs(programs: Iterable[PinellasCFGrantProgram]) -> list[Chunk]:
    return [chunk_pinellas_cf_program(p) for p in programs]


# --- ARPA funding allocations ------------------------------------------------


def chunk_arpa_allocation(allocation: ArpaFundingAllocation) -> Chunk:
    text = f"{allocation.category} — {allocation.amount_text}: {allocation.description}"

    source_url = allocation.attribution.source_url
    # No single field on ArpaFundingAllocation is independently unique
    # (two allocations could in principle share an amount_text across
    # categories) - category + amount_text together is the safe row
    # identity, confirmed unique across all 10 real allocations live.
    chunk_id = make_chunk_id(
        source_url, "arpa_allocation", allocation.category, allocation.amount_text
    )

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_ARPA_ALLOCATION,
        text=text,
        section_label=f"{allocation.category}: {allocation.amount_text}",
        attribution=allocation.attribution,
    )


def chunk_arpa_allocations(allocations: Iterable[ArpaFundingAllocation]) -> list[Chunk]:
    return [chunk_arpa_allocation(a) for a in allocations]
