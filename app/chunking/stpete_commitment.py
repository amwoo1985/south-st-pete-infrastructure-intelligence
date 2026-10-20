"""Chunking for stpete.org's ACCC action-item work plan
(app/crawlers/stpete_commitment.py's StpeteCommitmentActionItem,
DECISIONS #46/#47).

Boundary call: verified against the real recorded fixture
(tests/fixtures/stpete_commitment/st_petes_commitment.html) rather than
assumed - the 16 real action items (10 Building Sector, 6 Transportation
Sector) are each a self-contained, independent sentence (e.g. "Deep energy
efficiency retrofits and retro-commissioning of municipal facilities
(F)"), not near-duplicate numeric fragments like the AMI table's rows
(DECISIONS #61). One item = one chunk, same row-based reasoning as
DECISIONS #62.

Identity-field tension (flagged, not papered over): unlike Pinellas CF
(program_name) or ARPA (category+amount_text), StpeteCommitmentActionItem
has no field independent of its own `text` that identifies a specific
item - no item number, no name. Two options were on the table:

1. Hash/prefix `text` itself into the identity - rejected outright: this
   is exactly what DECISIONS #58 forbids (identity derived from the
   chunk's own text content), and it defeats the whole point - a light
   prose edit to an item (a typo fix, a reworded clause) would mint a
   brand-new chunk_id instead of updating the existing one, the opposite
   of idempotent upsert.
2. Positional identity - (sector, ordinal-within-sector) - the same shape
   of ID DECISIONS #62 explicitly rejected for Pinellas CF/ARPA rows, on
   the grounds that a source reorder unrelated to content would silently
   reassign IDs.

This module uses option 2, but the DECISIONS #62 rejection doesn't
transfer cleanly, and that's worth being explicit about rather than
quietly inheriting the same conclusion for a different reason: Pinellas
CF/ARPA are listing-style tables where a reorder (alphabetizing,
re-sorting by a CMS admin) is plausible and would be purely
presentational, unrelated to content. st_petes_commitment.php is one
static, hand-authored HTML page - changing its order means literally
hand-editing that page's markup, so an incidental, content-unrelated
reorder is far less likely here. Still, the honest caveat: inserting one
new item in the middle of a sector's list (a real, plausible edit to a
living work plan) would shift every later item's ordinal and silently
reassign their chunk_ids, even though those items' own text never
changed - a real limitation, not a solved problem. This module accepts
that tradeoff because no better identity field exists; it is the lesser
of two flawed options, not a clean answer, and this docstring says so
instead of the code silently picking one without comment.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.stpete_commitment import StpeteCommitmentActionItem

DOC_TYPE_COMMITMENT_ACTION_ITEM = "stpete_commitment_action_item"


def chunk_stpete_commitment_action_item(
    item: StpeteCommitmentActionItem, ordinal_within_sector: int
) -> Chunk:
    """One action-item <li>. `ordinal_within_sector` is this item's 1-based
    position among its own sector's items, in document order - the
    positional identity component described in the module docstring."""
    text = f"St. Pete's Commitment — {item.sector} ({item.tier_label} tier). {item.text}"

    source_url = item.attribution.source_url
    chunk_id = make_chunk_id(
        source_url, "commitment_action_item", item.sector, str(ordinal_within_sector)
    )
    section_label = (
        f"St. Pete's Commitment — {item.sector}, Item {ordinal_within_sector} ({item.tier_label})"
    )

    return Chunk(
        chunk_id=chunk_id,
        doc_type=DOC_TYPE_COMMITMENT_ACTION_ITEM,
        text=text,
        section_label=section_label,
        attribution=item.attribution,
    )


def chunk_stpete_commitment_action_items(
    items: Iterable[StpeteCommitmentActionItem],
) -> list[Chunk]:
    """Chunks a full crawl's worth of action items, computing each item's
    ordinal within its own sector in document order (see
    chunk_stpete_commitment_action_item / module docstring)."""
    chunks: list[Chunk] = []
    ordinal_by_sector: dict[str, int] = {}
    for item in items:
        ordinal = ordinal_by_sector.get(item.sector, 0) + 1
        ordinal_by_sector[item.sector] = ordinal
        chunks.append(chunk_stpete_commitment_action_item(item, ordinal))
    return chunks
