"""Chunking for POST /documents/upload (app/api/documents.py) — Phase H.

Handles the three accepted upload shapes (PDF via pypdf, DOCX via
python-docx, TXT via plain decode), each normalized to the same
paragraph-based, size-capped grouping strategy before producing
app.chunking.base.Chunk objects, doc_type="uploaded_document".

Chunking boundary (directive): group consecutive paragraphs up to
CHUNK_TARGET_SIZE_CHARS, but NEVER split a single paragraph across two
chunks even if that one paragraph alone exceeds the cap — an oversized
single-paragraph chunk is accepted over fracturing mid-thought, per
.claude/rules/rag.md's chunking-boundary rule. See
_group_paragraphs_by_size(). CHUNK_TARGET_SIZE_CHARS=2000 is a reasoned
starting point loosely anchored to this project's existing real
chunk-size scale (DECISIONS #92 measured the 95th percentile of the 8
existing crawler-specific chunkers' OUTPUT sizes at ~2,288 chars) — it is
NOT a value measured against real uploaded-document paragraph lengths,
since no uploaded-document samples existed at build time and this
chunker is the first thing in this codebase to face that population.
Revisit once real uploads accumulate.

The never-split rule above has its own upper bound, enforced separately
(rag-review pre-commit finding, this round): a single paragraph/group
that still exceeds MAX_SAFE_CHUNK_TEXT_CHARS after normal grouping would
otherwise raise EmbeddingInputTooLargeError inside
app.embeddings.pipeline's embed call — and because chunk_ids here are
100% deterministic from file_hash+position, that failure would be
IDENTICAL on every retry, permanently stuck with no path to success
short of hand-editing the source document. See
_split_oversized_text_if_needed() for the bounded, sentence-boundary-
preferred (hard-character-window as a last resort) escape hatch, used
ONLY when a group is already over that ceiling — the normal case
(paragraphs under CHUNK_TARGET_SIZE_CHARS) never touches it.

DOCX-specific: when `Heading N`-styled paragraphs are present, they are
treated as section boundaries — same reasoning as
app/chunking/stpete_pages.py's h2-per-chunk precedent for ambiguous
document structure. Content is grouped by size *within* each heading
section, so a very long section can still produce more than one chunk,
each still labeled back to its heading. PDF, TXT, and DOCX-without-
headings all fall back to treating the whole document as one section.

Identity/attribution (directive, already decided): Attribution.now() with
source_url=f"upload://{file_hash}" and published_date=None (an uploaded
document has no independently known publish date). chunk_id is derived
from file_hash + a document-global, position-based group index (plus the
heading text when known, for readability/debugging) via make_chunk_id —
never from chunk text — so re-processing the same bytes (the 'failed'-row
retry path in app/api/documents.py) always reproduces the same chunk_ids
and upserts cleanly via app.embeddings.pipeline.embed_and_insert_chunk's
existing ON CONFLICT logic, rather than duplicating rows.

A completely empty/whitespace-only parsed document raises
EmptyDocumentError rather than silently producing zero chunks
(.claude/rules/crawler.md's fail-loud rule, applied here to uploaded
content exactly as it applies to crawled content) — the upload endpoint
surfaces this as the upload's failure_reason.

Known, deliberately-deferred edge case: a DOCX heading immediately
followed by another heading (or by end-of-document) with zero body
paragraphs in between produces no chunk for that heading at all — an
empty section is filtered out before chunking, same as any other
all-whitespace input. A document consisting ONLY of such bare headings
(no body text anywhere) would raise EmptyDocumentError. This is judged
rare enough for this domain (grant guidelines, CBA drafts, meeting
memos — real prose documents, not bare outlines) not to be worth the
added complexity of treating a lone heading as a valid one-line chunk;
flagged here rather than silently left unhandled.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO

from docx import Document as DocxDocument
from pypdf import PdfReader

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.base import Attribution
from app.embeddings.client import MAX_INPUT_CHARS as _EMBEDDING_MAX_INPUT_CHARS

logger = logging.getLogger("chunking.uploaded_document")

DOC_TYPE = "uploaded_document"

# See module docstring for the reasoning (a starting point anchored to
# this project's existing chunk-size scale, not a value measured against
# real uploaded-document samples).
CHUNK_TARGET_SIZE_CHARS = 2000

# Safety ceiling for the oversized-paragraph fallback split (see module
# docstring / _split_oversized_text_if_needed below). Margin under
# app.embeddings.client.MAX_INPUT_CHARS (the real embeddings-API
# pre-flight ceiling) accounts for the heading-text prefix
# chunk_uploaded_document folds into a DOCX section's text below — a bare
# paragraph just under MAX_INPUT_CHARS could still push the final
# heading+paragraph text over that real ceiling if this safety check ran
# without any headroom.
_OVERSIZED_TEXT_SAFETY_MARGIN_CHARS = 500
MAX_SAFE_CHUNK_TEXT_CHARS = _EMBEDDING_MAX_INPUT_CHARS - _OVERSIZED_TEXT_SAFETY_MARGIN_CHARS

# Joins grouped paragraphs back into one chunk's text, and is counted as
# part of a group's running size (see _group_paragraphs_by_size) so the
# cap reflects the chunk's real final byte size, not just its paragraphs'
# raw lengths summed.
_PARAGRAPH_JOIN = "\n\n"

# Blank-line-separated paragraph splitting, used for PDF page text and
# plain TXT content (DOCX gets its paragraph boundaries directly from
# python-docx's own paragraph objects, not this regex).
_BLANK_LINE_SPLIT = re.compile(r"\n\s*\n+")

# Sentence-boundary split for the oversized-text fallback below —
# deliberately simple (no NLP sentence tokenizer dependency added for a
# rare last-resort path): splits after a '.', '!', or '?' followed by
# whitespace. Good enough to avoid an arbitrary mid-word/mid-sentence cut
# in the common case; the hard character-window fallback in
# _split_oversized_text_if_needed covers the case where this finds no
# usable boundary at all.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


class EmptyDocumentError(ValueError):
    """Raised when an uploaded document parses to zero non-empty
    paragraphs — .claude/rules/crawler.md's fail-loud rule applied to
    uploaded content: never silently produce zero chunks."""


def _split_into_paragraphs(text: str) -> list[str]:
    return [block.strip() for block in _BLANK_LINE_SPLIT.split(text) if block.strip()]


def _paragraphs_from_pdf(raw_bytes: bytes) -> list[str]:
    """Joins every page's extracted text into ONE document string before
    splitting into paragraphs — NOT split-per-page-then-flattened
    (rag-review pre-commit finding, this round). A clause spanning a page
    break — routine in agenda packets and grant guidelines — has no blank
    line at the page boundary, so joining first and running
    _BLANK_LINE_SPLIT once globally correctly merges it back into one
    paragraph; splitting per page first would force it into two
    "paragraphs" before _group_paragraphs_by_size ever saw them,
    defeating the never-split-mid-thought guarantee at exactly the
    boundary it exists to protect. A genuine paragraph break that happens
    to fall at a page end still splits correctly either way, since the
    blank-line pattern is preserved across the join."""
    reader = PdfReader(BytesIO(raw_bytes))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    full_text = "\n".join(page_texts)
    return _split_into_paragraphs(full_text)


def _paragraphs_from_txt(raw_bytes: bytes) -> list[str]:
    # errors="replace" rather than strict decoding: an uploaded TXT file
    # of unknown origin failing the whole upload over one bad byte isn't
    # worth it for this domain (grant/CBA text documents) — a handful of
    # replacement characters in an otherwise-good document is a better
    # failure mode than rejecting the upload outright.
    text = raw_bytes.decode("utf-8", errors="replace")
    return _split_into_paragraphs(text)


def _sections_from_docx(raw_bytes: bytes) -> list[tuple[str | None, list[str]]]:
    """Returns an ordered list of (heading_text_or_None, [paragraph, ...])
    sections. Every `Heading N`-styled paragraph starts a new section;
    content before the first heading (or the whole document, if there are
    no headings at all) is one heading_text=None section — same shape the
    PDF/TXT fallback below always produces, so downstream grouping logic
    doesn't need to special-case DOCX."""
    document = DocxDocument(BytesIO(raw_bytes))

    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_paragraphs: list[str] = []
    heading_count = 0
    paragraph_count = 0

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        paragraph_count += 1
        style_name = paragraph.style.name if paragraph.style is not None else ""
        is_heading = style_name.startswith("Heading")
        if is_heading:
            heading_count += 1
            sections.append((current_heading, current_paragraphs))
            current_heading = text
            current_paragraphs = []
        else:
            current_paragraphs.append(text)

    sections.append((current_heading, current_paragraphs))

    # Silent-degradation visibility (rag-review follow-up, this round):
    # `startswith("Heading")` only matches Word's own default style
    # names. A DOCX exported from Google Docs/LibreOffice, or a non-
    # English Word install, can use different style names entirely and
    # this falls back to whole-document-as-one-section without erroring
    # — not wrong (the plain grouping fallback still produces valid
    # chunks), but worth a visible trace rather than silently degrading
    # for anything past a trivially short document.
    if heading_count == 0 and paragraph_count > 5:
        logger.debug(
            "DOCX has %d non-empty paragraph(s) but zero `Heading N`-styled "
            "paragraphs detected — falling back to whole-document size-based "
            "grouping with no section boundaries (see _sections_from_docx's "
            "docstring for style names this only recognizes)",
            paragraph_count,
        )

    return sections


def _group_paragraphs_by_size(paragraphs: list[str], *, target_chars: int) -> list[list[str]]:
    """Groups consecutive paragraphs up to `target_chars`, never splitting
    a single paragraph (see module docstring). A paragraph alone longer
    than target_chars becomes its own one-paragraph group rather than
    being fractured."""
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        paragraph_len = len(paragraph)
        added_len = paragraph_len if not current else len(_PARAGRAPH_JOIN) + paragraph_len
        if current and current_len + added_len > target_chars:
            groups.append(current)
            current = [paragraph]
            current_len = paragraph_len
        else:
            current.append(paragraph)
            current_len += added_len

    if current:
        groups.append(current)

    return groups


def _split_oversized_text_if_needed(
    text: str, *, max_chars: int = MAX_SAFE_CHUNK_TEXT_CHARS
) -> list[str]:
    """Last-resort bounded split for text that still exceeds `max_chars`
    after normal paragraph grouping (rag-review pre-commit finding, this
    round — see module docstring). Returns `[text]` unchanged in the
    overwhelmingly common case where it already fits.

    Sentence-boundary split preferred (splits after '.', '!', or '?'
    followed by whitespace, re-grouped up to `max_chars` the same way
    _group_paragraphs_by_size groups paragraphs) so a degraded chunk
    still reads as whole sentences. Falls back to a hard fixed-size
    character window ONLY for a piece that has no usable sentence
    boundary at all (e.g. an OCR/extraction artifact, or one truly
    enormous run-on sentence) — the true escape hatch, guaranteeing every
    returned piece is embeddable regardless of input shape."""
    if len(text) <= max_chars:
        return [text]

    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s]

    pieces: list[str] = []
    if len(sentences) > 1:
        current = ""
        for sentence in sentences:
            candidate = f"{current} {sentence}" if current else sentence
            if current and len(candidate) > max_chars:
                pieces.append(current)
                current = sentence
            else:
                current = candidate
        if current:
            pieces.append(current)
    else:
        # No usable sentence boundary at all — the whole text is one
        # "sentence" as far as _SENTENCE_SPLIT is concerned.
        pieces = [text]

    # Hard fallback: any piece STILL over max_chars (one sentence longer
    # than max_chars on its own, or no sentence boundaries existed)
    # gets a fixed-size character split. This is the only place in this
    # module that ever splits mid-word/mid-sentence — reached only when
    # sentence-level splitting couldn't bring a piece under the real
    # embeddings-API ceiling.
    final: list[str] = []
    for piece in pieces:
        if len(piece) <= max_chars:
            final.append(piece)
        else:
            for start in range(0, len(piece), max_chars):
                final.append(piece[start : start + max_chars])
    return final


def chunk_uploaded_document(
    raw_bytes: bytes,
    *,
    filename: str,
    file_hash: str,
    kind: str,
    target_chars: int = CHUNK_TARGET_SIZE_CHARS,
) -> list[Chunk]:
    """Parses `raw_bytes` per `kind` ("pdf" | "docx" | "txt" — already
    validated by the caller, app/api/documents.py's upload route) and
    returns a list of Chunk objects ready for
    app.embeddings.pipeline.embed_and_insert_chunk.

    Raises EmptyDocumentError if the document parses to zero non-empty
    paragraphs. Raises whatever pypdf/python-docx raise for a genuinely
    corrupt file (not caught here — the caller, the background upload
    task, is what turns any exception into the upload's stored
    failure_reason)."""
    if kind == "pdf":
        sections = [(None, _paragraphs_from_pdf(raw_bytes))]
    elif kind == "txt":
        sections = [(None, _paragraphs_from_txt(raw_bytes))]
    elif kind == "docx":
        sections = _sections_from_docx(raw_bytes)
    else:
        raise ValueError(f"unsupported kind {kind!r} — expected 'pdf', 'docx', or 'txt'")

    non_empty_sections = [(heading, paras) for heading, paras in sections if paras]
    if not non_empty_sections:
        raise EmptyDocumentError(
            f"{filename!r} parsed to zero non-empty paragraphs — nothing to chunk"
        )

    attribution = Attribution.now(f"upload://{file_hash}")
    chunks: list[Chunk] = []
    group_index = 0

    for heading_text, paragraphs in non_empty_sections:
        groups = _group_paragraphs_by_size(paragraphs, target_chars=target_chars)
        section_chunk_count = len(groups)

        for position, group in enumerate(groups, start=1):
            text = _PARAGRAPH_JOIN.join(group)

            if heading_text:
                # Fold the heading into the embedded text itself, not just
                # section_label — same reasoning as
                # app/chunking/stpete_pages.py's h2-per-chunk precedent
                # (its own module docstring: a chunk pulled out of a
                # multi-section page should still name which section it
                # came from). Omitting this would leave a DOCX section's
                # embedding blind to what the section is even about,
                # weakening retrieval for a query phrased using words
                # from the heading itself (e.g. "eligibility") that don't
                # happen to also appear in the body paragraphs.
                text = f"{heading_text}\n\n{text}"
                section_label = (
                    heading_text
                    if section_chunk_count == 1
                    else f"{heading_text} (part {position})"
                )
            else:
                # No heading text available (PDF, TXT, or a DOCX section
                # with no heading styling) — filename is not part of the
                # document's own semantic content, so it's used only for
                # section_label/citation display, never folded into the
                # embedded text itself.
                section_label = (
                    filename if section_chunk_count == 1 else f"{filename} (part {position})"
                )

            # Position-based identity (group_index is document-global and
            # monotonically increasing) is already fully deterministic and
            # collision-free on its own; the heading text is folded in
            # too purely for a more legible chunk_id derivation trail
            # (matches the spec's "chunk-group index or heading text +
            # occurrence-disambiguation" — using both costs nothing).
            identity_base = f"{heading_text or ''}#{group_index}"

            # Last-resort bounded split (rag-review pre-commit finding,
            # this round — see module docstring): a `text` that's STILL
            # over the real embeddings-API ceiling after normal grouping
            # (only possible for a group holding a single oversized
            # paragraph — CHUNK_TARGET_SIZE_CHARS is always far below
            # MAX_SAFE_CHUNK_TEXT_CHARS, so a multi-paragraph group can
            # never reach it) gets split into embeddable pieces here,
            # rather than reaching embed_and_insert_chunks and raising
            # EmbeddingInputTooLargeError on a chunk_id that would be
            # byte-identical (and therefore fail identically) on every
            # retry. `pieces` is `[text]` unchanged in the normal case.
            pieces = _split_oversized_text_if_needed(text)
            is_degraded_split = len(pieces) > 1
            if is_degraded_split:
                logger.warning(
                    "chunk_uploaded_document: a %d-char group in %r exceeded "
                    "the %d-char safety ceiling after normal grouping — "
                    "applying the sentence/hard-split fallback into %d "
                    "piece(s) rather than failing the whole upload",
                    len(text),
                    filename,
                    MAX_SAFE_CHUNK_TEXT_CHARS,
                    len(pieces),
                )

            for piece_index, piece_text in enumerate(pieces, start=1):
                piece_label = (
                    # Distinct wording ("split") from the normal
                    # size-grouping "(part N)" suffix, per the
                    # orchestrator's directive: a citation should make it
                    # visible that this was a degraded mid-paragraph
                    # split, not a natural paragraph/grouping boundary.
                    f"{section_label} (split {piece_index}/{len(pieces)})"
                    if is_degraded_split
                    else section_label
                )
                identity = (
                    f"{identity_base}:split{piece_index}" if is_degraded_split else identity_base
                )
                chunk_id = make_chunk_id(f"upload://{file_hash}", "uploaded_document", identity)

                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        doc_type=DOC_TYPE,
                        text=piece_text,
                        section_label=piece_label,
                        attribution=attribution,
                    )
                )
            group_index += 1

    return chunks
