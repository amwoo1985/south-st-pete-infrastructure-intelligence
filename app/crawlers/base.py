"""Shared base for every Tier-1/Tier-1.5 crawler.

Binding rules this module exists to enforce (see .claude/rules/crawler.md,
.claude/rules/data.md, DECISIONS #11):

- robots.txt is checked before every fetch, never assumed.
- Requests are rate-limited per host with a real delay, not a comment.
- The User-Agent is honest and identifies this tool/contact — never a
  spoofed browser string.
- The source host allow-list mirrors DECISIONS #11 exactly; fetching any
  other host raises immediately instead of silently working.
- A structure-parsing failure raises CrawlerStructureError (fail loud) —
  callers must never let a broken parser return an empty result that looks
  like "nothing new happened".
- Every item a crawler produces carries an Attribution: source URL,
  retrieval timestamp (always known at fetch time), and an optional
  (nullable, per .claude/rules/data.md) published/effective date.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import requests

logger = logging.getLogger("crawler")

# Honest, non-spoofed identification. Never mimic a browser User-Agent to
# evade blocking (.claude/rules/crawler.md) — if a site blocks this, that's
# a signal to stop and flag it, not to circumvent it.
DEFAULT_USER_AGENT = (
    "SouthStPeteInfraIntel/0.1 "
    "(+civic research tool for the South St. Petersburg Energy Coalition "
    "CBA negotiation; contact: blaqcoffeeblaqtea@gmail.com)"
)

# Respectful default spacing between requests to the same host. See
# DECISIONS #14 for why 2.0s.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 2.0

DEFAULT_TIMEOUT_SECONDS = 30

# A narrow, host-specific exception to RobotsChecker's otherwise-strict
# 404-only "no restrictions declared" rule (DECISIONS #16). For these hosts
# ONLY, a 403 fetching robots.txt itself is also treated as "no
# restrictions declared" instead of raising RobotsError. This is NOT a
# general loosening of the 404-only rule — every other host's 403 (or any
# other non-404 error) still raises RobotsError exactly as before.
#
# archive-video.granicus.com is here per DECISIONS #89/#91: three live
# curl attempts under three different User-Agents (this project's honest
# UA, no UA, and a browser-like UA+Referer) all returned a bare 403 on
# /robots.txt specifically — never a 200 with a real policy, and never a
# clean 404. The two response shapes seen (a CloudFront bot-filter page,
# and a separate S3-style AccessDenied XML body) are both consistent with
# this CDN's bucket simply not having a robots.txt object at all and
# S3/CloudFront masking a missing key as AccessDenied/403 rather than a
# clean 404 — a common S3 static-hosting quirk, not a real published
# disallow policy. Do not add another host to this set without a new
# DECISIONS.md entry documenting equivalent evidence for that host.
ROBOTS_403_TREATED_AS_PERMISSIVE_HOSTS = frozenset(
    {
        "archive-video.granicus.com",  # DECISIONS #89/#91
    }
)

# The closed source list, mirrored from DECISIONS #11 exactly. Adding a
# host here without a new DECISIONS.md entry first is the scope violation
# crawler-review checks for — don't do it.
ALLOWED_SOURCE_HOSTS = frozenset(
    {
        "pinellas.legistar.com",  # Tier 1 — Pinellas County BCC (Legistar)
        "stpete.org",  # Tier 1 — City of St. Petersburg grants/loans (index page, DECISIONS #11)
        "www.stpete.org",  # Tier 1 — the 6 category sub-pages named in DECISIONS #30
        "pinellascf.org",  # Tier 1 — Pinellas Community Foundation grants
        # NOTE: stpete.granicus.com is deliberately NOT listed. #77 found its
        # robots.txt blocks this project's honest User-Agent from the entire
        # host; #79 redesigned Granicus discovery around human-supplied MP3
        # URLs specifically so no code ever needs to fetch this host again.
        # Do not re-add it without a new DECISIONS entry.
        "www.stpeteha.org",  # Tier 1 — St. Petersburg Housing Authority, the 9 pages named in DECISIONS #48/#49
        "pinellas.gov",  # Tier 1 — Pinellas County Housing & Community Development, the 11 pages named in DECISIONS #48/#52 (NOT pinellascounty.org — that host 301-redirects here, see DECISIONS #48)
        "archive-video.granicus.com",  # Tier 1.5 — Granicus MP3/video CDN (#76). Fetched by the Day 5+ worker (not built yet) to download audio for meetings registered via #79-81's human-input mechanism. This host was never blocked (#77 only found stpete.granicus.com blocked). See crawler.md's Granicus-specific section for the CDN's own bot-filtering behavior (browser-like User-Agent + Referer required, confirmed CDN filtering not real access control).
    }
)


class ScopeViolationError(Exception):
    """Raised when a crawler tries to fetch a host outside DECISIONS #11's
    closed source list. Never catch-and-continue this — it means either a
    bug or an undocumented scope expansion."""


class RobotsError(Exception):
    """Raised when robots.txt disallows a fetch, or when it cannot be
    verified at all (network/HTTP failure other than 404). Never treated as
    equivalent to 'nothing found' — an unverifiable robots.txt fails loud
    rather than being silently assumed permissive."""


class CrawlerStructureError(Exception):
    """Raised when a page/PDF doesn't contain the HTML/text structure a
    parser expects. This is the fail-loud contract from
    .claude/rules/crawler.md — never swallow this into an empty result."""


@dataclass(frozen=True)
class Attribution:
    """Mandatory provenance metadata every crawled item carries.

    - source_url: always known, the exact URL the item was fetched/derived
      from.
    - retrieval_timestamp: always known at fetch time (UTC, tz-aware) — not
      nullable.
    - published_date: the meeting/effective date. Nullable, per
      .claude/rules/data.md's nullable-over-sentinel rule, because it may
      not be parseable yet at capture time for every source shape.
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


class RateLimiter:
    """Real per-host throttle — sleeps as needed, not a comment promising
    politeness."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._last_request_at: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_request_at.get(host)
        if last is not None:
            remaining = self._min_interval - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at[host] = time.monotonic()


class RobotsChecker:
    """Fetches and caches robots.txt per host, using the same honest
    User-Agent and the same rate limiter as regular fetches. Absence of a
    robots.txt (404) is treated as "no restrictions declared" per standard
    convention; any other fetch failure raises RobotsError rather than
    silently assuming permission.

    Narrow exception (DECISIONS #89/#91): for hosts listed in
    ROBOTS_403_TREATED_AS_PERMISSIVE_HOSTS, a 403 on the robots.txt fetch
    itself is ALSO treated as "no restrictions declared". Every other host,
    and every other non-2xx status on any of these hosts, still raises
    RobotsError exactly as before — this is a per-host allowlist for one
    specific status code on one specific fetch, not a general loosening."""

    def __init__(
        self,
        session: requests.Session,
        user_agent: str,
        rate_limiter: RateLimiter,
    ) -> None:
        self._session = session
        self._user_agent = user_agent
        self._rate_limiter = rate_limiter
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _get_parser(self, url: str) -> urllib.robotparser.RobotFileParser:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key in self._parsers:
            return self._parsers[host_key]

        robots_url = f"{host_key}/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)

        self._rate_limiter.wait(parsed.netloc)
        try:
            resp = self._session.get(
                robots_url,
                headers={"User-Agent": self._user_agent},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise RobotsError(
                f"Could not fetch robots.txt at {robots_url}: {exc}"
            ) from exc

        host_is_403_exception = parsed.netloc in ROBOTS_403_TREATED_AS_PERMISSIVE_HOSTS
        if resp.status_code == 404 or (resp.status_code == 403 and host_is_403_exception):
            # No robots.txt published: no restrictions declared. Standard
            # convention for 404, not an assumption made without evidence.
            # The 403 branch only fires for the narrow, documented
            # per-host allowlist above (DECISIONS #89/#91) — every other
            # host's 403 falls through to the RobotsError branch below,
            # exactly as before.
            parser.parse([])
        elif resp.status_code >= 400:
            raise RobotsError(
                f"robots.txt fetch at {robots_url} returned HTTP {resp.status_code}"
            )
        else:
            parser.parse(resp.text.splitlines())

        self._parsers[host_key] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        parser = self._get_parser(url)
        return parser.can_fetch(self._user_agent, url)


class BaseCrawler:
    """Shared fetch infrastructure for every Tier-1/Tier-1.5 crawler.
    Subclasses implement their own parsing; they should call
    ``self.fetch(url)`` for every HTTP request so robots.txt checking,
    rate-limiting, scope enforcement, and the honest User-Agent are applied
    uniformly."""

    def __init__(
        self,
        source_name: str,
        user_agent: str = DEFAULT_USER_AGENT,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.source_name = source_name
        self.user_agent = user_agent
        self._session = session or requests.Session()
        self._rate_limiter = RateLimiter(min_request_interval_seconds)
        self._robots = RobotsChecker(self._session, user_agent, self._rate_limiter)
        self._logger = logging.getLogger(f"crawler.{source_name}")

    def fetch(self, url: str, *, method: str = "GET", **kwargs) -> requests.Response:
        host = urlparse(url).netloc
        if host not in ALLOWED_SOURCE_HOSTS:
            raise ScopeViolationError(
                f"{host} is not in the closed source list (DECISIONS #11): {url}"
            )

        if not self._robots.can_fetch(url):
            self._logger.error("robots.txt disallows fetching %s", url)
            raise RobotsError(f"robots.txt disallows fetching {url}")

        self._rate_limiter.wait(host)

        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", self.user_agent)
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT_SECONDS)

        resp = self._session.request(method, url, headers=headers, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp

    def fail_loud(self, message: str) -> None:
        """Log and raise on a structure-parsing failure. Never call this
        and then continue as if nothing happened; never catch
        CrawlerStructureError and substitute an empty result."""
        self._logger.error("[%s] structure error: %s", self.source_name, message)
        raise CrawlerStructureError(f"[{self.source_name}] {message}")
