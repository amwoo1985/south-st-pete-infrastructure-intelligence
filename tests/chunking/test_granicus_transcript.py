"""Tests for app/chunking/granicus_transcript.py — the missing link
found during the Phase C small-batch validation run (DECISIONS #116):
completed Granicus transcripts were never chunked/embedded until this
module existed."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.chunking.granicus_transcript import (
    DOC_TYPE,
    EmptyTranscriptError,
    chunk_granicus_transcript,
)

MP3_URL = "https://archive-video.granicus.com/stpete/stpete_test-uuid.mp3"
MEDIAPLAYER_URL = "https://stpete.granicus.com/MediaPlayer.php?view_id=14&clip_id=9999"
RETRIEVAL_TS = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _chunk(text: str, **overrides):
    kwargs = dict(
        mp3_url=MP3_URL,
        meeting_title="Test Committee",
        transcript_text=text,
        source_url=MEDIAPLAYER_URL,
        published_date=date(2026, 4, 9),
        retrieval_timestamp=RETRIEVAL_TS,
    )
    kwargs.update(overrides)
    return chunk_granicus_transcript(**kwargs)


def test_short_transcript_produces_one_chunk():
    chunks = _chunk("Roll call. Curtis, here. Motion carries. Meeting adjourned.")
    assert len(chunks) == 1
    assert chunks[0].doc_type == DOC_TYPE
    assert chunks[0].section_label == "Test Committee"
    assert "Roll call" in chunks[0].text


def test_long_transcript_splits_at_sentence_boundaries_not_mid_sentence():
    # One sentence repeated enough times to force multiple groups under a
    # small target_chars — every chunk's text must still consist of whole
    # sentences (end in ./!/? with no truncation mid-word).
    sentence = "This is a real sentence about the budget committee agenda item. "
    text = sentence * 20
    chunks = _chunk(text, target_chars=200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.strip().endswith(".")
        # No chunk should start mid-word (i.e. every chunk starts with the
        # sentence's own first word, "This").
        assert chunk.text.strip().startswith("This is a real sentence")


def test_chunk_labels_include_part_number_when_multiple():
    sentence = "Another real sentence for the transcript grouping test. "
    text = sentence * 20
    chunks = _chunk(text, target_chars=200)
    assert chunks[0].section_label == "Test Committee (part 1)"
    assert chunks[1].section_label == "Test Committee (part 2)"


def test_chunk_id_deterministic_across_reruns():
    text = "Roll call. Curtis, here. Motion carries."
    first = _chunk(text)
    second = _chunk(text)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_chunk_id_derived_from_mp3_url_and_position_not_text():
    # Same mp3_url/position, different text — chunk_id must be identical
    # (chunk_id is never derived from the chunk's own content, per
    # app/chunking/base.py's make_chunk_id rule).
    chunks_a = _chunk("Roll call. Motion carries.")
    chunks_b = _chunk("A completely different sentence here. Adjourned.")
    assert chunks_a[0].chunk_id == chunks_b[0].chunk_id


def test_attribution_is_passthrough_not_rederived():
    chunks = _chunk(
        "Roll call. Motion carries.",
        source_url=MEDIAPLAYER_URL,
        published_date=date(2025, 12, 25),
        retrieval_timestamp=RETRIEVAL_TS,
    )
    attribution = chunks[0].attribution
    assert attribution.source_url == MEDIAPLAYER_URL
    assert attribution.published_date == date(2025, 12, 25)
    assert attribution.retrieval_timestamp == RETRIEVAL_TS


def test_empty_transcript_raises_not_silently_empty():
    with pytest.raises(EmptyTranscriptError):
        _chunk("   ")


def test_single_oversized_sentence_hard_split_not_stuck_forever():
    # A single "sentence" (no terminal ./!/? anywhere inside it) longer
    # than the real embeddings-API ceiling — plausible raw whisper-1
    # output for a long stretch of roll-call names or number readouts
    # with no clean punctuation. Must not become one unbounded chunk that
    # would fail identically on every retry (rag-review finding,
    # DECISIONS #118).
    from app.chunking.granicus_transcript import MAX_SAFE_CHUNK_TEXT_CHARS

    oversized = ("word " * (MAX_SAFE_CHUNK_TEXT_CHARS // 5 + 500)).strip() + "."
    assert len(oversized) > MAX_SAFE_CHUNK_TEXT_CHARS

    chunks = _chunk(oversized)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= MAX_SAFE_CHUNK_TEXT_CHARS
    assert chunks[0].section_label.endswith(f"(split 1/{len(chunks)})")
    # Every piece still round-trips back to the original text in order.
    assert "".join(c.text for c in chunks) == oversized


def test_no_paragraph_structure_still_chunks_correctly():
    # Real whisper-1 output has NO blank lines at all (verified live
    # against two real transcripts, per this module's docstring) — a
    # single continuous block must still chunk correctly with zero
    # paragraph breaks anywhere in the input.
    text = "Sentence one here. Sentence two here. Sentence three here."
    assert "\n\n" not in text
    chunks = _chunk(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
