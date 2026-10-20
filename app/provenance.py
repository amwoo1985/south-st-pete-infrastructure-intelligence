"""Source attribution carried by every chunk in the corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class Attribution:
    """Mandatory provenance metadata every ingested item carries.

    - source_url: always known — where the item came from (an upload's
      synthetic URI, a document link).
    - retrieval_timestamp: always known at ingest time (UTC, tz-aware) — not
      nullable.
    - published_date: the document's own effective date. Nullable, per
      .claude/rules/data.md's nullable-over-sentinel rule, because it may
      not be known at ingest time.
    """

    source_url: str
    retrieval_timestamp: datetime
    published_date: date | None = None

    @classmethod
    def now(cls, source_url: str, published_date: date | None = None) -> "Attribution":
        return cls(
            source_url=source_url,
            retrieval_timestamp=datetime.now(timezone.utc),
            published_date=published_date,
        )
