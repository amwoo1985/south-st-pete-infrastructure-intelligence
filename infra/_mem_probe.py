"""One-off, committed diagnostic (DECISIONS #138) — real peak-memory
evidence for audio_split.py's decode+split path against a real oversized
Granicus file. Same one-off-tool convention as infra/diag_granicus.py.
Run inside the migrate container via a base64 payload passed through
infra/run_migration_task.sh (its arg-array builder splits on newlines, so
a multi-line script can't go through as a plain `python -c` argument).

Polls real cgroup memory usage (whichever of the v1/v2 paths exist) on a
background thread while downloading + decoding + splitting a real file,
reporting the true peak memory usage across the WHOLE container (parent
Python process + any ffmpeg child pydub spawns) — this is what Fargate's
OOM killer actually enforces against, not just one process's own maxrss.

Never touches granicus_transcription_jobs or any other DB table — reads
only the mp3_url passed in as an argument, downloads it, and measures.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from app.crawlers.base import BaseCrawler
from app.granicus.audio_split import split_audio_for_whisper
from app.granicus.worker import WHISPER_MAX_FILE_BYTES, _download_ranged

URL = sys.argv[1]

_CGROUP_CANDIDATES = [
    "/sys/fs/cgroup/memory.current",  # cgroup v2
    "/sys/fs/cgroup/memory/memory.usage_in_bytes",  # cgroup v1
]


def _read_mem_bytes() -> int | None:
    for path in _CGROUP_CANDIDATES:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            continue
    return None


_peak = [0]
_poll_error: list[BaseException] = []
_stop = threading.Event()


def _poll() -> None:
    # deploy-review pre-commit finding, MEDIUM: the original version only
    # caught FileNotFoundError/ValueError inside _read_mem_bytes — any
    # other exception (e.g. PermissionError on a locked-down cgroup path)
    # would kill this daemon thread silently, leaving PEAK_CONTAINER_
    # MEMORY_BYTES understated with no fail-loud signal, even though this
    # single number is the entire evidentiary basis for a real sizing
    # decision. Broadened to catch and record any exception, checked by
    # the main script before it ever prints a peak figure as trustworthy.
    try:
        while not _stop.is_set():
            v = _read_mem_bytes()
            if v is not None and v > _peak[0]:
                _peak[0] = v
            time.sleep(0.5)
    except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed
        _poll_error.append(exc)


poller = threading.Thread(target=_poll, daemon=True)
poller.start()

crawler = BaseCrawler(source_name="granicus-worker")
tmp_dir = Path(tempfile.gettempdir())
# deploy-review pre-commit finding, MEDIUM: a fixed "memprobe.mp3" name
# (and the split pieces derived from it, since audio_split.py's split_id
# is audio_path.stem) would collide if this diagnostic is ever run twice
# concurrently in the same container — the real worker/audio_split.py
# path already solved this exact problem (worker.py's own
# granicus-{uuid4().hex}.mp3 convention); this script now matches it.
dest = tmp_dir / f"memprobe-{uuid4().hex}.mp3"

try:
    t0 = time.time()
    total_bytes = _download_ranged(crawler, URL, dest)
    t1 = time.time()
    print(f"downloaded {total_bytes} bytes in {t1 - t0:.1f}s, peak_bytes_so_far={_peak[0]}")

    result = split_audio_for_whisper(dest, tmp_dir, max_piece_bytes=WHISPER_MAX_FILE_BYTES)
    t2 = time.time()
    print(
        f"split into {len(result.pieces)} pieces, total_duration_s={result.total_duration_seconds:.1f} "
        f"({result.total_duration_seconds / 60:.1f} min), decode+split took {t2 - t1:.1f}s"
    )

    for p in result.pieces:
        sz = p.path.stat().st_size
        print(f"  piece {p.index}: {sz} bytes ({p.path.name})")
        p.path.unlink(missing_ok=True)
finally:
    # deploy-review pre-commit finding, LOW (.claude/rules/data.md
    # best-effort-cleanup-on-partial-failure): split_audio_for_whisper
    # already cleans up its own piece files on its own failure path (see
    # its docstring), but this script's own downloaded `dest` file was
    # left behind on any exception before this fix — now cleaned up
    # regardless of which step failed.
    dest.unlink(missing_ok=True)

_stop.set()
poller.join(timeout=2)

if _poll_error:
    # Fail loud rather than silently print a peak figure that may be
    # understated because the poller died partway through (deploy-review
    # finding above) — this number is load-bearing for a real memory-
    # sizing decision and must not be trusted past a known measurement gap.
    print(f"POLLER_ERROR (peak figure below may be UNDERSTATED): {_poll_error[0]!r}")

print(f"PEAK_CONTAINER_MEMORY_BYTES={_peak[0]} ({_peak[0] / 1024 / 1024:.1f} MiB)")
print(f"total_file_bytes={total_bytes} observed_bitrate_bps={(total_bytes * 8) / result.total_duration_seconds:.0f}")
