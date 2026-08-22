"""Granicus meeting-transcription registration subsystem.

DECISIONS #79 redesigned this pipeline's *discovery* step (which meeting
has which MP3 URL) to human-supplied input, after #77 found
stpete.granicus.com's robots.txt blocks this project's honest User-Agent
from the entire host. This package owns that human-input registration
path only:

- app.granicus.schema: the ``granicus_transcription_jobs`` table DDL
  (DECISIONS #81).
- app.granicus.register: validates and inserts one meeting as a
  ``status='pending'`` row (DECISIONS #80-82), including the 12-month
  backfill bound (DECISIONS #12, #82).

It does NOT claim, fetch, or transcribe anything — that's the Day 5+
polling worker (not built yet), which will consume the rows this package
inserts.
"""

from __future__ import annotations
