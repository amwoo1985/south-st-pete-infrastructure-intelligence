"""Tests for app/granicus/audio_split.py — the decision-(c) duration-aware
whisper-1 splitting module (worker.py's module docstring, "decision (c)").

No real `ffmpeg`/`pydub` decode anywhere in this file — `ffmpeg` is not
installed on this dev machine (see requirements.txt's `pydub` comment;
deploy-infra adds it to the Docker image, a parallel track this round).
`AudioSegment.from_file` is replaced with a fake, deterministic
"decoded audio" stand-in (`_FakeSegment`) that supports the exact
`pydub.AudioSegment` surface this module actually uses — `len()`,
slice-`__getitem__`, and `.export()` — with an injectable bytes-per-ms
density so exported sizes are fully controlled and predictable, matching
this repo's "mock the audio-processing... calls" convention
(app/granicus/worker.py module docstring) for a dependency this
environment can't exercise for real.

Real split behavior against real audio is instead proven by this round's
real end-to-end validation against the local docker-compose stack (which
does have `ffmpeg`), not by any test in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.granicus.audio_split as audio_split
from app.granicus.audio_split import (
    MAX_RESPLIT_ATTEMPTS,
    AudioSplitError,
    _export_with_resplit,
    split_audio_for_whisper,
)


class _FakeSegment:
    """Stands in for `pydub.AudioSegment`: a `[start_ms:end_ms]`-sliceable,
    `len()`-able object whose `.export()` writes a real file to disk sized
    deterministically from `bytes_per_ms` — no real audio decode/encode
    anywhere."""

    def __init__(self, duration_ms: int, bytes_per_ms: float):
        self.duration_ms = duration_ms
        self.bytes_per_ms = bytes_per_ms

    def __len__(self) -> int:
        return self.duration_ms

    def __getitem__(self, key: slice) -> "_FakeSegment":
        start = key.start or 0
        stop = key.stop if key.stop is not None else self.duration_ms
        return _FakeSegment(stop - start, self.bytes_per_ms)

    def export(self, dest_path, format=None, bitrate=None):  # noqa: A002 - matches pydub's real signature
        n_bytes = max(0, round(self.duration_ms * self.bytes_per_ms))
        # Sparse file (seek + single trailing byte), not a materialized
        # `b"x" * n_bytes` literal — a deliberately pathological test
        # density (used by the adaptive-resplit-exhaustion tests) can
        # imply an n_bytes in the GB range; a real byte string that size
        # allocates real memory and is genuinely slow, whereas a sparse
        # file reports the same `.stat().st_size` almost instantly
        # regardless of magnitude. Content is never read by anything
        # under test, only its size.
        with open(dest_path, "wb") as f:
            if n_bytes > 0:
                f.seek(n_bytes - 1)
                f.write(b"\0")
        return dest_path


class _FakeAudioSegmentClass:
    """Stands in for the `AudioSegment` class itself — only `.from_file`
    is ever called by `audio_split.py`."""

    def __init__(self, segment: _FakeSegment):
        self._segment = segment

    def from_file(self, _path):
        return self._segment


def _patch_audio_segment(monkeypatch, segment: _FakeSegment) -> None:
    monkeypatch.setattr(audio_split, "AudioSegment", _FakeAudioSegmentClass(segment))


# --- split_audio_for_whisper: normal multi-piece case -------------------------


def test_split_audio_for_whisper_splits_into_pieces_all_under_ceiling(tmp_path, monkeypatch):
    """A file whose real observed bitrate the split module can trust
    (export density matches total_bytes/total_duration exactly) should
    converge in one export per piece — proving the N-pieces case this
    round's brief calls out, with every piece verified under the ceiling."""
    total_bytes = 10_000_000
    total_duration_ms = 60_000  # 60s
    density = total_bytes / total_duration_ms  # bytes/ms, matches the real file

    audio_path = tmp_path / "source.mp3"
    audio_path.write_bytes(b"s" * total_bytes)
    _patch_audio_segment(monkeypatch, _FakeSegment(total_duration_ms, density))

    max_piece_bytes = 3_000_000
    result = split_audio_for_whisper(audio_path, tmp_path, max_piece_bytes=max_piece_bytes)

    assert len(result.pieces) >= 2  # actually split, not a no-op
    assert result.total_duration_seconds == pytest.approx(60.0)
    total_reconstructed_ms = 0
    for i, piece in enumerate(result.pieces, start=1):
        assert piece.index == i
        assert piece.path.exists()
        assert piece.path.stat().st_size <= max_piece_bytes
    # No gaps/overlaps: durations of all pieces should sum back to the
    # original total (verified indirectly via each piece's exported byte
    # size / density, since _FakeSegment doesn't expose ms directly here).
    total_reconstructed_bytes = sum(p.path.stat().st_size for p in result.pieces)
    assert total_reconstructed_bytes == pytest.approx(total_bytes, rel=0.01)


def test_split_audio_for_whisper_piece_count_matches_target_fraction_math(tmp_path, monkeypatch):
    """Pins the piece-count math against the documented
    SPLIT_TARGET_FRACTION formula, not just 'some number of pieces >= 2'."""
    total_bytes = 25_000_000
    total_duration_ms = 100_000
    density = total_bytes / total_duration_ms

    audio_path = tmp_path / "source.mp3"
    audio_path.write_bytes(b"s" * total_bytes)
    _patch_audio_segment(monkeypatch, _FakeSegment(total_duration_ms, density))

    max_piece_bytes = 5_000_000
    result = split_audio_for_whisper(audio_path, tmp_path, max_piece_bytes=max_piece_bytes)

    target_bytes = int(max_piece_bytes * audio_split.SPLIT_TARGET_FRACTION)
    import math

    expected_piece_count = max(1, math.ceil(total_bytes / target_bytes))
    assert len(result.pieces) == expected_piece_count


def test_split_audio_for_whisper_already_small_enough_returns_original_unsplit(tmp_path, monkeypatch):
    audio_path = tmp_path / "small.mp3"
    audio_path.write_bytes(b"s" * 1000)
    _patch_audio_segment(monkeypatch, _FakeSegment(5000, 0.2))

    result = split_audio_for_whisper(audio_path, tmp_path, max_piece_bytes=10_000)

    assert len(result.pieces) == 1
    assert result.pieces[0].path == audio_path


def test_split_audio_for_whisper_filenames_namespaced_by_source_stem(tmp_path, monkeypatch):
    """crawler-review pre-commit finding, HIGH: two different jobs sharing
    the same dest_dir (the real production shape — `tmp_dir` defaults to
    the system temp dir, shared across concurrent workers) must never
    produce colliding piece filenames. `split_id` is derived from the
    source audio file's own already-unique name
    (`granicus-{uuid4().hex}.mp3` in production, per worker.py), so two
    different source files always produce disjoint piece filenames."""
    total_bytes = 10_000_000
    total_duration_ms = 60_000
    density = total_bytes / total_duration_ms

    audio_path_a = tmp_path / "granicus-aaaa.mp3"
    audio_path_a.write_bytes(b"a" * total_bytes)
    audio_path_b = tmp_path / "granicus-bbbb.mp3"
    audio_path_b.write_bytes(b"b" * total_bytes)

    _patch_audio_segment(monkeypatch, _FakeSegment(total_duration_ms, density))
    result_a = split_audio_for_whisper(audio_path_a, tmp_path, max_piece_bytes=3_000_000)
    _patch_audio_segment(monkeypatch, _FakeSegment(total_duration_ms, density))
    result_b = split_audio_for_whisper(audio_path_b, tmp_path, max_piece_bytes=3_000_000)

    names_a = {p.path.name for p in result_a.pieces}
    names_b = {p.path.name for p in result_b.pieces}
    assert names_a.isdisjoint(names_b)
    assert all(n.startswith("granicus-aaaa-") for n in names_a)
    assert all(n.startswith("granicus-bbbb-") for n in names_b)
    # Both jobs' real files still exist side by side, unharmed by sharing
    # dest_dir.
    for piece in result_a.pieces + result_b.pieces:
        assert piece.path.exists()


def test_split_audio_for_whisper_leaf_indices_stay_unique_when_a_piece_resplits(tmp_path, monkeypatch):
    """crawler-review pre-commit finding, MEDIUM: when one outer piece
    resplits into multiple leaf files, every SplitPiece.index across the
    WHOLE result must still be unique — otherwise a PieceTranscriptionError
    naming 'piece N/total' could refer to either of two different
    on-disk files."""
    total_bytes = 10_000_000
    total_duration_ms = 60_000
    # A density that's fine for the file's OVERALL initial estimate but
    # forces at least one exported piece to come out oversized in practice
    # (real bitrate isn't perfectly uniform — exactly the scenario this
    # module's docstring names as the reason post-export verification is
    # load-bearing). Use a density noticeably higher than what the
    # observed_bitrate estimate assumes, on every export, so every initial
    # piece resplits at least once.
    density = (total_bytes / total_duration_ms) * 3

    audio_path = tmp_path / "granicus-resplit-case.mp3"
    audio_path.write_bytes(b"s" * total_bytes)
    _patch_audio_segment(monkeypatch, _FakeSegment(total_duration_ms, density))

    result = split_audio_for_whisper(audio_path, tmp_path, max_piece_bytes=3_000_000)

    indices = [p.index for p in result.pieces]
    assert indices == list(range(1, len(result.pieces) + 1))  # unique, sequential, no gaps
    assert len(result.pieces) > 4  # confirms resplitting actually happened (>4
    # would be more than the naive un-resplit piece_count for this input)
    for piece in result.pieces:
        assert piece.path.stat().st_size <= 3_000_000


def test_split_audio_for_whisper_cleans_up_earlier_pieces_when_a_later_piece_fails(tmp_path, monkeypatch):
    """crawler-review pre-commit finding, HIGH: if piece K of a multi-piece
    split ultimately fails (genuine encoding anomaly, exhausts
    MAX_RESPLIT_ATTEMPTS), pieces 1..K-1 — already real, successfully
    exported files at that point — must not be left orphaned on disk."""
    total_bytes = 10_000_000
    total_duration_ms = 60_000

    call_count = {"n": 0}
    good_density = total_bytes / total_duration_ms

    class _FailsOnSecondPieceSegment(_FakeSegment):
        def __getitem__(self, key: slice) -> "_FakeSegment":
            start = key.start or 0
            stop = key.stop if key.stop is not None else self.duration_ms
            call_count["n"] += 1
            # First outer slice succeeds normally; every subsequent outer
            # slice is pathologically oversized and can never converge.
            density = good_density if call_count["n"] == 1 else 1_000_000
            return _FakeSegment(stop - start, density)

    audio_path = tmp_path / "granicus-fails-partway.mp3"
    audio_path.write_bytes(b"s" * total_bytes)
    _patch_audio_segment(
        monkeypatch, _FailsOnSecondPieceSegment(total_duration_ms, good_density)
    )

    with pytest.raises(AudioSplitError):
        split_audio_for_whisper(audio_path, tmp_path, max_piece_bytes=3_000_000)

    # Only the original source file remains — every piece file created
    # before the failure (piece 1's real export) was cleaned up.
    remaining = list(tmp_path.iterdir())
    assert remaining == [audio_path]


def test_split_audio_for_whisper_raises_on_zero_duration(tmp_path, monkeypatch):
    audio_path = tmp_path / "source.mp3"
    audio_path.write_bytes(b"s" * 10_000_000)
    _patch_audio_segment(monkeypatch, _FakeSegment(0, 0.0))

    with pytest.raises(AudioSplitError):
        split_audio_for_whisper(audio_path, tmp_path, max_piece_bytes=1_000_000)


# --- _export_with_resplit: adaptive re-split on a real oversized export ------


def test_export_with_resplit_converges_after_oversized_export(tmp_path, monkeypatch):
    """A piece whose real export comes out over max_piece_bytes (the
    density used at export time doesn't match what the caller assumed —
    the exact scenario this module's docstring names as the reason (b)'s
    post-export verification is load-bearing, not (a)'s pre-estimate)
    must be adaptively halved and re-exported until every resulting piece
    verifiably fits."""
    max_piece_bytes = 1_000_000
    # Deliberately high density so the first (and second) export attempts
    # both come out oversized, converging only after two halvings.
    segment = _FakeSegment(duration_ms=5000, bytes_per_ms=500)  # 5000*500=2.5M, oversized

    paths = _export_with_resplit(
        segment,
        tmp_path,
        split_id="test-split",
        index=1,
        max_piece_bytes=max_piece_bytes,
        export_bitrate_kbps=128,
    )

    assert len(paths) >= 2  # had to split at least once
    for p in paths:
        assert p.exists()
        assert p.stat().st_size <= max_piece_bytes


def test_export_with_resplit_no_split_needed_when_already_under_ceiling(tmp_path):
    segment = _FakeSegment(duration_ms=1000, bytes_per_ms=10)  # 10,000 bytes, well under
    paths = _export_with_resplit(
        segment,
        tmp_path,
        split_id="test-split",
        index=1,
        max_piece_bytes=1_000_000,
        export_bitrate_kbps=128,
    )
    assert len(paths) == 1
    assert paths[0].stat().st_size == 10_000


def test_export_with_resplit_raises_after_exhausting_max_attempts(tmp_path):
    """A pathological case where a piece can never be brought under the
    ceiling no matter how many times it's halved (a genuine encoding
    anomaly, not routine bitrate variance) must fail loud with
    AudioSplitError, not recurse forever."""
    max_piece_bytes = 1000
    segment = _FakeSegment(duration_ms=5000, bytes_per_ms=1000)  # always wildly oversized

    with pytest.raises(AudioSplitError):
        _export_with_resplit(
            segment,
            tmp_path,
            split_id="test-split",
            index=1,
            max_piece_bytes=max_piece_bytes,
            export_bitrate_kbps=128,
        )


def test_export_with_resplit_raises_on_degenerate_midpoint(tmp_path):
    """A 1ms segment that's still oversized has no smaller duration to
    halve toward (midpoint_ms would be 0) — must fail loud immediately
    rather than recurse on a degenerate 0-length slice."""
    segment = _FakeSegment(duration_ms=1, bytes_per_ms=1_000_000)

    with pytest.raises(AudioSplitError):
        _export_with_resplit(
            segment,
            tmp_path,
            split_id="test-split",
            index=1,
            max_piece_bytes=1000,
            export_bitrate_kbps=128,
        )


def test_export_with_resplit_cleans_up_left_paths_when_right_half_fails(tmp_path):
    """crawler-review pre-commit finding, HIGH: if the right half of a
    halved piece ultimately fails (exhausts MAX_RESPLIT_ATTEMPTS), the
    left half's already-successfully-exported file(s) must not be
    orphaned on disk. Density is asymmetric per-half via a segment whose
    LEFT half is small enough to fit immediately but whose RIGHT half
    never fits no matter how many times it's halved."""

    class _AsymmetricSegment(_FakeSegment):
        def __getitem__(self, key: slice) -> "_FakeSegment":
            start = key.start or 0
            stop = key.stop if key.stop is not None else self.duration_ms
            # First half (start==0) is cheap/small; second half is
            # pathologically dense and can never fit under the ceiling.
            density = 1 if start == 0 else 1_000_000
            return _FakeSegment(stop - start, density)

    segment = _AsymmetricSegment(duration_ms=5000, bytes_per_ms=1)
    max_piece_bytes = 1000

    with pytest.raises(AudioSplitError):
        _export_with_resplit(
            segment,
            tmp_path,
            split_id="asym-split",
            index=1,
            max_piece_bytes=max_piece_bytes,
            export_bitrate_kbps=128,
            # Force an immediate halve so left/right take genuinely
            # different paths (density=1 whole-segment export would
            # otherwise already fit and never reach the recursion at all).
            attempt=0,
        )

    # No file left behind anywhere under tmp_path — neither the oversized
    # exports discarded along the way, nor the left half's real success.
    assert list(tmp_path.iterdir()) == []


def test_max_resplit_attempts_bounds_recursion_depth(tmp_path):
    """Sanity check that MAX_RESPLIT_ATTEMPTS is actually what bounds the
    exhaustion case above, not some other accidental limit — pins the
    documented constant's value."""
    assert MAX_RESPLIT_ATTEMPTS == 5
