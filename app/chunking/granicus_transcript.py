"""Chunking for completed Granicus meeting transcripts
(`granicus_transcription_jobs.transcript_text`, `app/granicus/worker.py`).

Closes a real gap found during the Phase C small-batch validation run
(DECISIONS #116): two real meetings transcribed successfully, but nothing
in this codebase ever chunked/embedded a Granicus transcript — PLAN.md's
own Phase D scope names "transcript-with-timestamps" as one of the three
content shapes this project's chunking layer is supposed to handle, and
it was never built. This module is that missing piece.

Content shape (verified against the two real transcripts produced this
round, not assumed): `whisper-1`'s default response is one continuous
block of plain text with sentence punctuation but NO paragraph breaks —
confirmed live, `"\\n\\n" not in transcript_text` for both. This means
`app/chunking/uploaded_document.py`'s blank-line paragraph split has
nothing to split on here; this module groups at SENTENCE boundaries
instead (reusing the same "group up to a target size, prefer a natural
boundary" shape, adapted to the boundary this content shape actually
has).

Timestamps (`Chunk.start_seconds`/`end_seconds`, `app/chunking/base.py`):
still NOT populated by this module, and that's not an oversight — the
transcription call in `app/granicus/worker.py`'s `_transcribe_audio`
requests `whisper-1`'s default response format, which returns plain
`.text` with no segment-level timing at all. Populating these fields for
real would mean changing that call to request `verbose_json` (a bigger,
separate change with its own re-transcription-cost and format
implications for already-completed jobs) — out of scope for closing
today's gap, which is "transcripts exist but were never chunked at all,"
not "chunks lack timestamps" (already a known, accepted, documented state
per `app/chunking/base.py`'s own docstring).

Identity/attribution: chunk_id is derived from `mp3_url` + a
document-global position index (never from the transcript text itself) —
so re-chunking the same completed job (e.g. after a future re-run)
reproduces the same chunk_ids and upserts cleanly via
`app.embeddings.pipeline.embed_and_insert_chunk`'s existing ON CONFLICT
logic, the same idempotency shape every other chunker in this codebase
uses (DECISIONS #58). Attribution is NOT re-derived here — it's a direct
passthrough of the values `app/granicus/register.py` already recorded at
registration time (`source_url` = the MediaPlayer.php URL, `published_date`
= the real meeting date, `retrieval_timestamp` = when the meeting was
registered) via `app.crawlers.base.Attribution`, matching this project's
existing "chunking never re-derives or drops attribution" rule.

A transcript that parses to zero non-whitespace content raises
EmptyTranscriptError rather than silently producing zero chunks
(`.claude/rules/crawler.md`'s fail-loud rule, same discipline
`uploaded_document.py` already applies to uploaded content) — in
practice this should never happen for a `status='completed'` row (the
worker only marks a job completed after a real transcription succeeded),
but the same defensive check every other chunker in this codebase applies
to its own "should never be empty" input is applied here too, rather than
assumed away.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from app.chunking.base import Chunk, make_chunk_id
from app.crawlers.base import Attribution
from app.embeddings.client import MAX_INPUT_CHARS as _EMBEDDING_MAX_INPUT_CHARS

logger = logging.getLogger("chunking.granicus_transcript")

DOC_TYPE = "granicus_transcript"

# Matches app/chunking/uploaded_document.py's CHUNK_TARGET_SIZE_CHARS
# convention (DECISIONS #92's measured ~2,288-char 95th percentile across
# this project's other chunkers) — no transcript-specific measurement
# exists yet (only 2 real transcripts exist as of this module's build),
# so this starts from the same project-wide anchor rather than a fresh
# guess. Revisit once more real transcripts accumulate.
CHUNK_TARGET_SIZE_CHARS = 2000

# Oversized-fragment safety valve (rag-review pre-commit finding, DECISIONS
# #118) — same reasoning as app/chunking/uploaded_document.py's
# MAX_SAFE_CHUNK_TEXT_CHARS: a single "sentence" this regex never finds a
# terminal mark in (a long stretch of roll-call names, number readouts, or
# just mumbled speech with no clean punctuation — plausible raw whisper-1
# output) would otherwise become one unbounded chunk. Because chunk_id is
# 100% deterministic from (mp3_url, position), a chunk that trips
# EmbeddingInputTooLargeError would fail IDENTICALLY on every retry —
# permanently stuck, no path to success short of hand-editing the
# transcript. Margin under the real embeddings-API ceiling, not the ceiling
# itself, for the same reason uploaded_document.py keeps one.
_OVERSIZED_TEXT_SAFETY_MARGIN_CHARS = 500
MAX_SAFE_CHUNK_TEXT_CHARS = _EMBEDDING_MAX_INPUT_CHARS - _OVERSIZED_TEXT_SAFETY_MARGIN_CHARS

_PIECE_JOIN = " "

# Same simple, no-NLP-dependency sentence boundary as
# app/chunking/uploaded_document.py's _SENTENCE_SPLIT: splits after a
# '.', '!', or '?' followed by whitespace. Whisper's plain-text output
# reliably punctuates sentences even with no paragraph structure, so this
# boundary is the right level for this content shape (verified against
# both real transcripts: every one has multiple sentence-ending marks).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


class EmptyTranscriptError(ValueError):
    """Raised when a transcript is empty/whitespace-only — should not
    happen for a real `status='completed'` row, but never silently
    produce zero chunks regardless (`.claude/rules/crawler.md`)."""


def _split_into_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _group_sentences_by_size(sentences: list[str], *, target_chars: int) -> list[list[str]]:
    """Groups consecutive sentences up to `target_chars`. A single
    sentence longer than target_chars on its own becomes its own group
    rather than being fractured mid-sentence — same never-split-the-
    atomic-unit rule `uploaded_document.py`'s paragraph grouping applies,
    one level down (sentences, not paragraphs, since that's the boundary
    this content shape has)."""
    groups: list[list[str]] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)
        added_len = sentence_len if not current else len(_PIECE_JOIN) + sentence_len
        if current and current_len + added_len > target_chars:
            groups.append(current)
            current = [sentence]
            current_len = sentence_len
        else:
            current.append(sentence)
            current_len += added_len

    if current:
        groups.append(current)

    return groups


def _split_oversized_group_if_needed(
    text: str, *, max_chars: int = MAX_SAFE_CHUNK_TEXT_CHARS
) -> list[str]:
    """Last-resort hard-character-window split for a group that still
    exceeds `max_chars` after normal sentence grouping (rag-review
    pre-commit finding, DECISIONS #118) — only reachable when a SINGLE
    sentence alone is that long, since `CHUNK_TARGET_SIZE_CHARS` is always
    far below `MAX_SAFE_CHUNK_TEXT_CHARS`, so a multi-sentence group can
    never reach it. No sentence-level or clause-level boundary is
    preferred here (unlike uploaded_document.py's paragraph-then-sentence
    fallback) because at this point there IS no finer natural boundary
    left to try — `_split_into_sentences` already found none inside this
    text. Returns `[text]` unchanged in the overwhelmingly common case."""
    if len(text) <= max_chars:
        return [text]
    return [text[start : start + max_chars] for start in range(0, len(text), max_chars)]


def chunk_granicus_transcript(
    *,
    mp3_url: str,
    meeting_title: str,
    transcript_text: str,
    source_url: str,
    published_date: date | None,
    retrieval_timestamp: datetime,
    target_chars: int = CHUNK_TARGET_SIZE_CHARS,
) -> list[Chunk]:
    """Chunks one completed Granicus transcription job
    (`granicus_transcription_jobs` row) into Chunk objects ready for
    `app.embeddings.pipeline.embed_and_insert_chunk`.

    Every argument here is a direct field from that row — this function
    does no DB access itself (matches every other chunker in this
    codebase: chunking is pure, callers own I/O)."""
    sentences = _split_into_sentences(transcript_text)
    if not sentences:
        raise EmptyTranscriptError(
            f"{mp3_url!r} ({meeting_title!r}) transcript_text has no usable sentences "
            "— nothing to chunk"
        )

    attribution = Attribution(
        source_url=source_url,
        retrieval_timestamp=retrieval_timestamp,
        published_date=published_date,
    )

    groups = _group_sentences_by_size(sentences, target_chars=target_chars)
    total_groups = len(groups)

    chunks: list[Chunk] = []
    for position, group in enumerate(groups, start=1):
        text = _PIECE_JOIN.join(group)
        section_label = (
            meeting_title if total_groups == 1 else f"{meeting_title} (part {position})"
        )

        # Oversized-fragment escape hatch (see _split_oversized_group_if_needed).
        # `pieces` is `[text]` unchanged in the normal case.
        pieces = _split_oversized_group_if_needed(text)
        is_degraded_split = len(pieces) > 1
        if is_degraded_split:
            logger.warning(
                "chunk_granicus_transcript: a %d-char group in %r (position %d) exceeded "
                "the %d-char safety ceiling — applying the hard-split fallback into %d "
                "piece(s) rather than producing a chunk that would fail identically on "
                "every retry",
                len(text),
                meeting_title,
                position,
                MAX_SAFE_CHUNK_TEXT_CHARS,
                len(pieces),
            )

        for piece_index, piece_text in enumerate(pieces, start=1):
            piece_label = (
                f"{section_label} (split {piece_index}/{len(pieces)})"
                if is_degraded_split
                else section_label
            )
            identity = f"{position}:split{piece_index}" if is_degraded_split else str(position)
            chunk_id = make_chunk_id(mp3_url, DOC_TYPE, identity)
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_type=DOC_TYPE,
                    text=piece_text,
                    section_label=piece_label,
                    attribution=attribution,
                )
            )

    return chunks
