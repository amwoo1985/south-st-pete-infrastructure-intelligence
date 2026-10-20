"""Frame-safe, duration-aware audio splitting for whisper-1's real 25 MiB
single-request limit — the authorized follow-up to DECISIONS #86's
"decision (b): fail loud, no split" (see `app/granicus/worker.py`'s module
docstring, "decision (c)", for the full history of why (b) was chosen
first and what changed to authorize this).

## Why decoded-domain slicing, not a raw byte-offset split

MP3 frames aren't fixed-length — a raw byte cut of the compressed stream
can land mid-frame and corrupt both halves (DECISIONS #86's original,
still-valid objection to a naive split). `pydub.AudioSegment` decodes the
file to raw PCM first (via `ffmpeg`); slicing a decoded `AudioSegment`
with `[start_ms:end_ms]` cuts at sample-accurate boundaries in the
decoded-audio domain, and `.export()` re-encodes each resulting slice as
its own independent, fully-valid MP3 from scratch. There is no byte-offset
cut of a compressed stream anywhere in this module.


## Split-sizing strategy: initial real-bitrate estimate + per-piece
## post-export verification with adaptive re-split (a hybrid of the two
## strategies this round's brief posed as alternatives)

Compressed export size is not perfectly predictable from decoded duration
alone — VBR-adjacent encoder behavior, ID3/container overhead, and
per-segment content density (a stretch of near-silence compresses smaller
than dense speech even at a nominally constant target bitrate) all mean a
duration-only estimate can miss. Two pure strategies were on the table:

  (a) Compute a target piece duration from the file's own real observed
      bitrate (`total_bytes / total_duration`) with a safety factor, and
      trust it.
  (b) Split by a guessed duration and verify each exported piece's actual
      on-disk byte size afterward, adaptively re-splitting anything still
      oversized.

Pure (a) alone is a plausible-but-unverified guess — real export size
could still drift over the ceiling for a bitrate-atypical piece, and this
codebase's own real-world evidence to date (DECISIONS #116's measured
137.7kbps, consistent across three real full-length council meetings) is
itself just one number, not proof every future meeting's audio is equally
uniform end-to-end. Pure (b) alone (starting from an
arbitrary/naive initial guess) would waste encode passes converging
piece-by-piece with no informed starting point.

**Built: a hybrid, and (b)'s verification is what actually makes
correctness hold — (a) is only ever used to make the first guess cheap.**
The file's own real observed bitrate (`total_bytes * 8 / total_duration`)
sizes the *initial* per-piece target duration, so the common case (a
real, roughly-constant-bitrate meeting recording) converges in one export
per piece with no wasted work. Every exported piece's actual on-disk byte
size is then verified against the true `max_piece_bytes` ceiling
regardless of what the initial estimate predicted; a piece that comes out
oversized is recursively halved (in the *decoded* domain, then
re-exported) and re-verified, bounded at `MAX_RESPLIT_ATTEMPTS` (below).
This is why (b) is the load-bearing guarantee: even if (a)'s starting
guess is wrong for a given piece, the piece that reaches the caller is
always proven to fit, not just estimated to fit.

Halving one oversized piece in place (rather than recomputing a new
global piece count and re-exporting the *entire* file) is deliberately
localized: a bitrate anomaly in one piece doesn't invalidate every other
already-correctly-sized piece's export.


## `SPLIT_TARGET_FRACTION = 0.9`: the initial per-piece budget

The initial target duration is sized to `max_piece_bytes * 0.9` (~22.5
MiB out of the real 25 MiB ceiling), not the full ceiling itself — a 10%
margin absorbs realistic per-export overhead (container/ID3 headers,
encoder framing, rounding) so the common case doesn't immediately trip
the adaptive re-split path on a near-miss. This margin is a convenience
that reduces how often the (b) verification loop has to actually do a
second pass, not the thing that guarantees correctness — that guarantee
is the post-export byte-size check itself, which applies regardless of
this fraction's value.


## `MAX_RESPLIT_ATTEMPTS = 5`: bound on adaptive re-splitting

Each re-split halves a piece's duration, so (assuming a piece's export
size scales roughly linearly with its duration, which real near-constant-
bitrate speech audio does) each retry roughly halves the exported size
too — 5 attempts gives up to a 2**5 = 32x size reduction from the initial
estimate, a generous multiple of what DECISIONS #116's single real
137.7kbps-consistent measurement suggests should ever be needed (0-1
retries in the realistic case). Bounding this at all — rather than
recursing until the byte size happens to fit — exists to fail loud on a
genuine anomaly (a corrupted decode, a pathological non-audio file that
slipped past registration) rather than spin toward a degenerate
near-zero-length "piece" forever; `AudioSplitError` is raised if the bound
is exhausted, or if a piece's decoded duration hits zero mid-halving
before the byte size ever satisfies the ceiling (division-by-zero /
infinite-regress guard).


## Export format and bitrate: `mp3`, explicit `bitrate=` matching the
## source

Each piece is re-exported as `mp3` (whisper-1 supports it directly, no
downstream format-conversion step needed) with an explicit
`bitrate=f"{observed_kbps}k"` matching the SOURCE file's own real
measured bitrate, rather than accepting `ffmpeg`'s default mp3 encoder
bitrate (which may not match the source at all, e.g. a common ~128kbps
default that could be higher OR lower than a given meeting's real
encoding — DECISIONS #116 measured a real 137.7kbps for St. Petersburg's
Granicus instance specifically, not a round default). Passing the real
observed bitrate explicitly is what makes the (a) initial-estimate half
of this module's strategy actually valid — the estimate is only as good
as the assumption that re-encoded pieces reproduce close to the source's
real bitrate, and leaving that to an unrelated encoder default would
silently break that assumption.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger("granicus.audio_split")

SPLIT_TARGET_FRACTION = 0.9
MAX_RESPLIT_ATTEMPTS = 5
EXPORT_FORMAT = "mp3"


class AudioSplitError(Exception):
    """Raised when this module cannot produce pieces that all fit under
    `max_piece_bytes` — a genuine anomaly (corrupted/degenerate decode, a
    non-audio file that slipped past registration), never silently
    swallowed into a partial or oversized result
    (`.claude/rules/crawler.md`'s fail-loud contract, applied here to
    audio processing rather than page/PDF parsing)."""


@dataclass(frozen=True)
class SplitPiece:
    """One exported, whisper-1-safe audio piece in chronological order."""

    path: Path
    index: int  # 1-based, chronological, and GLOBALLY UNIQUE across every
    # leaf piece a split call produces (crawler-review pre-commit finding,
    # MEDIUM — assigned once after all pieces are known, not reused from
    # the outer per-source-segment loop, so a resplit piece's multiple
    # leaf files never share an index). Used only for human-readable
    # failure messages ("piece 3/12 failed..."), never for stitching order
    # (pieces are always returned/consumed in list order already).


@dataclass(frozen=True)
class AudioSplitResult:
    pieces: list[SplitPiece]
    total_duration_seconds: float


def _export_with_resplit(
    segment: AudioSegment,
    dest_dir: Path,
    *,
    split_id: str,
    index: int,
    max_piece_bytes: int,
    export_bitrate_kbps: int,
    attempt: int = 0,
    name_prefix: str = "piece",
) -> list[Path]:
    """Exports `segment`, verifies its real on-disk byte size against
    `max_piece_bytes`, and recursively halves + re-exports if it's still
    oversized. See module docstring — this verify-then-adaptively-resplit
    step is what actually guarantees every returned piece fits, not the
    initial duration estimate that produced `segment`.

    `split_id` (crawler-review pre-commit finding, HIGH) namespaces every
    exported filename to one `split_audio_for_whisper` call — without it,
    two concurrent workers processing two different jobs with the SAME
    shared `dest_dir` (the system temp dir, in production) would both
    start from `piece-001-a0.mp3` and could write to the literal same
    path mid-export. `claim_next_job`'s `FOR UPDATE SKIP LOCKED` already
    exists specifically to let concurrent workers safely process
    different jobs at once — this parameter keeps that property true for
    split-piece files too, matching the download step's own
    `granicus-{uuid4().hex}.mp3` uniqueness convention one level up.

    On a failure partway through the halving recursion (crawler-review
    pre-commit finding, HIGH), any sibling piece(s) already
    successfully exported before the failure are cleaned up before the
    exception propagates — never left orphaned on disk
    (`.claude/rules/data.md`'s best-effort-cleanup-on-partial-failure
    rule, applied here one recursion level at a time so cleanup cascades
    correctly regardless of which level actually fails).
    """
    if attempt >= MAX_RESPLIT_ATTEMPTS:
        raise AudioSplitError(
            f"piece {name_prefix}-{index} still exceeds {max_piece_bytes:,} bytes "
            f"after {MAX_RESPLIT_ATTEMPTS} adaptive re-split attempts — this is "
            "outside real-world bitrate variance seen so far (DECISIONS #116) and "
            "points at a genuine encoding anomaly, not routine bitrate drift"
        )

    dest_path = dest_dir / f"{split_id}-{name_prefix}-{index:03d}-a{attempt}.mp3"
    segment.export(dest_path, format=EXPORT_FORMAT, bitrate=f"{export_bitrate_kbps}k")
    actual_bytes = dest_path.stat().st_size

    if actual_bytes <= max_piece_bytes:
        return [dest_path]

    logger.warning(
        "audio split piece %s-%d exported at %d bytes, over the %d-byte ceiling "
        "(attempt %d/%d) — adaptively re-splitting this piece in half and retrying",
        name_prefix,
        index,
        actual_bytes,
        max_piece_bytes,
        attempt + 1,
        MAX_RESPLIT_ATTEMPTS,
    )
    dest_path.unlink(missing_ok=True)  # discard the oversized export, don't leave a stray file

    segment_len_ms = len(segment)
    midpoint_ms = segment_len_ms // 2
    if midpoint_ms <= 0 or midpoint_ms >= segment_len_ms:
        raise AudioSplitError(
            f"piece {name_prefix}-{index} cannot be halved further (decoded "
            f"duration {segment_len_ms}ms) but still exceeds the {max_piece_bytes:,}"
            "-byte ceiling — real encoding anomaly, not routine bitrate variance"
        )

    left = segment[:midpoint_ms]
    right = segment[midpoint_ms:]
    left_paths = _export_with_resplit(
        left,
        dest_dir,
        split_id=split_id,
        index=index,
        max_piece_bytes=max_piece_bytes,
        export_bitrate_kbps=export_bitrate_kbps,
        attempt=attempt + 1,
        name_prefix=f"{name_prefix}L",
    )
    try:
        right_paths = _export_with_resplit(
            right,
            dest_dir,
            split_id=split_id,
            index=index,
            max_piece_bytes=max_piece_bytes,
            export_bitrate_kbps=export_bitrate_kbps,
            attempt=attempt + 1,
            name_prefix=f"{name_prefix}R",
        )
    except Exception:
        # left_paths already succeeded and are real files on disk — clean
        # them up before propagating, so a failure on the right half
        # doesn't orphan the left half's exports (this except's own
        # caller, if any, cleans up ITS siblings the same way, cascading
        # correctly up the recursion regardless of which level fails).
        for p in left_paths:
            p.unlink(missing_ok=True)
        raise
    return left_paths + right_paths


def split_audio_for_whisper(
    audio_path: Path,
    dest_dir: Path,
    *,
    max_piece_bytes: int,
) -> AudioSplitResult:
    """Splits `audio_path` (already fully downloaded to disk) into
    frame-safe pieces each verified to be `<= max_piece_bytes` on disk,
    exported into `dest_dir`. Returns pieces in chronological order.

    Decodes the whole file via `pydub.AudioSegment.from_file` (wraps
    `ffmpeg`) — this is real, non-trivial local work for a multi-hour
    file, but is CPU-bound with no network wait, unlike the download step
    it follows.

    Raises `AudioSplitError` (never silently returns something wrong) if
    the decoded duration is degenerate (zero-length) or if any piece
    cannot be brought under `max_piece_bytes` within
    `MAX_RESPLIT_ATTEMPTS` — see module docstring. On any such failure,
    every piece file already successfully exported earlier in this call
    is cleaned up before the exception propagates (crawler-review
    pre-commit finding, HIGH — `.claude/rules/data.md`'s best-effort
    cleanup rule; a genuine encoding anomaly must not also leave orphaned
    `.mp3` files behind on disk on top of failing the job).
    """
    total_bytes = audio_path.stat().st_size
    if total_bytes <= max_piece_bytes:
        # Defensive only — real callers only invoke this module once
        # they've already confirmed the file exceeds max_piece_bytes, but
        # this function stays correct standalone regardless of caller
        # discipline.
        return AudioSplitResult(
            pieces=[SplitPiece(path=audio_path, index=1)],
            total_duration_seconds=len(AudioSegment.from_file(audio_path)) / 1000,
        )

    audio = AudioSegment.from_file(audio_path)
    total_duration_ms = len(audio)
    if total_duration_ms <= 0:
        raise AudioSplitError(
            f"{audio_path}: decoded duration is {total_duration_ms}ms — cannot "
            "split a zero-length/undecoded audio file"
        )

    observed_bitrate_bps = (total_bytes * 8) / (total_duration_ms / 1000)
    export_bitrate_kbps = max(1, round(observed_bitrate_bps / 1000))

    target_bytes = int(max_piece_bytes * SPLIT_TARGET_FRACTION)
    piece_count = max(1, math.ceil(total_bytes / target_bytes))
    piece_duration_ms = math.ceil(total_duration_ms / piece_count)

    # Namespaces every exported filename to THIS split call (crawler-review
    # pre-commit finding, HIGH) — see _export_with_resplit's docstring for
    # why a shared dest_dir across concurrent jobs otherwise risks a
    # filename collision.
    split_id = audio_path.stem

    piece_paths: list[Path] = []
    start_ms = 0
    outer_index = 0
    try:
        while start_ms < total_duration_ms:
            end_ms = min(start_ms + piece_duration_ms, total_duration_ms)
            segment = audio[start_ms:end_ms]
            outer_index += 1
            exported_paths = _export_with_resplit(
                segment,
                dest_dir,
                split_id=split_id,
                index=outer_index,
                max_piece_bytes=max_piece_bytes,
                export_bitrate_kbps=export_bitrate_kbps,
            )
            piece_paths.extend(exported_paths)
            start_ms = end_ms
    except Exception:
        for p in piece_paths:
            p.unlink(missing_ok=True)
        raise

    # Unique, sequential, chronological index per LEAF piece (crawler-review
    # pre-commit finding, MEDIUM) — assigned only after every piece is
    # known, not reused from the outer per-source-segment loop above. A
    # resplit piece produces MULTIPLE leaf files from one outer_index; if
    # SplitPiece.index reused that outer_index, two genuinely different
    # on-disk pieces could report the same index, making a
    # PieceTranscriptionError's "piece N/total" naming ambiguous about
    # which physical piece actually failed.
    pieces = [SplitPiece(path=p, index=i) for i, p in enumerate(piece_paths, start=1)]

    logger.info(
        "split %s (%d bytes, %.1f min, ~%dkbps observed) into %d whisper-1-safe "
        "piece(s) targeting ~%d bytes/piece",
        audio_path,
        total_bytes,
        total_duration_ms / 1000 / 60,
        export_bitrate_kbps,
        len(pieces),
        target_bytes,
    )

    return AudioSplitResult(pieces=pieces, total_duration_seconds=total_duration_ms / 1000)
