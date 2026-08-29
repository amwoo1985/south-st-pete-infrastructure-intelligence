"""Chunking for stpete.org's income_limits.php AMI (Area Median Income)
eligibility-threshold table (app/crawlers/stpete_income_limits.py's
AMIThreshold — DECISIONS #43/#44).

The crawler emits 120 rows (8 household sizes x 15 program-column pairs).
Chunking those 1:1 would produce 120 near-identical numeric-only rows with
no prose context and heavy redundancy — exactly the context-stuffing risk
.claude/rules/rag.md and the rag-review charter's point 2 warn about, and a
poor match for how this table is actually queried (see DECISIONS #61).
Instead, this groups by `program` (the field the crawler already resolves
a clean per-group effective date against) and produces one chunk per
program covering every household size and AMI-percent tier that program
appears in.

Framing sentence (DECISIONS #154): a real, reproduced retrieval failure
("What is St. Petersburg's AMI" returning not_in_corpus despite these 7
chunks existing) was root-caused to real, live-measured similarity scores
— the old chunk text (just "Area Median Income (AMI) eligibility
thresholds for <program>:" plus bare numeric rows) never once names "St.
Petersburg" or otherwise gives the embedding model a geography to anchor
to, so a query naming a place scored 0.36-0.40 against it, well under
RELEVANCE_THRESHOLD=0.5 (app/rag/retrieval.py) — even lower than DECISIONS
#73's own off-topic negative-control ceiling (0.42-0.46). The one new
sentence prepended below is chosen to be verifiably true and NOT a
jurisdiction claim: it states where the table is published (this
crawler's own one hard-coded URL, income_limits.php, is a
stpete.org/City-of-St.-Petersburg page — a fact about the source, not an
assertion that the dollar figures apply specifically to city vs. county
boundaries). This is deliberate: the live page itself never states
"Pinellas County" anywhere (confirmed live, DECISIONS #154), so adding a
county claim here would fabricate provenance this table doesn't actually
carry — exactly the kind of false-confidence citation
.claude/rules/rag.md exists to prevent, even though it might have scored
even higher. Real, live-measured effect of this one sentence (DECISIONS
#154): the two real failing queries went from 0.37/0.40 to 0.56/0.60
similarity against this exact chunk — comfortably above threshold — and
the already-passing "Pinellas County AMI" query improved too (0.57 ->
0.68), not regressed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.stpete_income_limits import AMIThreshold

DOC_TYPE_AMI_THRESHOLD = "ami_threshold_table"

_NO_PROGRAM_LABEL = "100% AMI (median income benchmark, not tied to a specific program)"
_NO_PROGRAM_IDENTITY_KEY = "no_program_100pct"


def chunk_ami_thresholds(thresholds: Iterable[AMIThreshold]) -> list[Chunk]:
    """One chunk per program (including the one bare-100%-AMI "no program"
    group), each a compact table of every (household_size, ami_percent) ->
    dollar_amount pair that program applies to. See module docstring /
    DECISIONS #61 for why this is grouped rather than 1:1 per row."""
    thresholds = list(thresholds)
    if not thresholds:
        return []

    groups: dict[str | None, list[AMIThreshold]] = defaultdict(list)
    for threshold in thresholds:
        groups[threshold.program].append(threshold)

    source_url = thresholds[0].attribution.source_url

    chunks: list[Chunk] = []
    for program, rows in groups.items():
        rows_sorted = sorted(rows, key=lambda r: (r.ami_percent, r.household_size))
        program_label = program if program is not None else _NO_PROGRAM_LABEL

        lines = [
            "This table appears on the City of St. Petersburg's official "
            "website (stpete.org), on its housing income-limits reference "
            "page. It lists Area Median Income (AMI) eligibility "
            f"thresholds for {program_label}:"
        ]
        for row in rows_sorted:
            lines.append(
                f"Household size {row.household_size}, {row.ami_percent}% AMI: "
                f"${row.dollar_amount:,}"
            )
        text = "\n".join(lines)

        identity_key = program if program is not None else _NO_PROGRAM_IDENTITY_KEY
        chunk_id = make_chunk_id(source_url, "ami_threshold_group", identity_key)

        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_type=DOC_TYPE_AMI_THRESHOLD,
                text=text,
                section_label=program_label,
                # Every row in a program's group shares that program's own
                # effective date (assigned once, per-program, by the
                # crawler — DECISIONS #44) - reusing the first row's
                # Attribution is exact, not an approximation.
                attribution=rows_sorted[0].attribution,
            )
        )

    return chunks
