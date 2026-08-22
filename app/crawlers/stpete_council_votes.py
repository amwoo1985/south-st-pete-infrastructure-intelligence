"""St. Petersburg City Council vote/agenda data via the "Council Minutes &
Action Taken" index page and its per-meeting numbered-agenda PDFs.

Authorized by DECISIONS #56, off DECISIONS #55 Thread 1's recon. Does NOT
authorize any `council_members`/`agenda_items`/`council_votes` database
schema or FK-link to the RAG chunk store — that punt (DECISIONS #54) still
stands. This module is crawl-layer only: fetch, parse, return attributed
Python objects.

Target index page (served under the already-allow-listed ``www.stpete.org``
host per DECISIONS #26/#31 — no ``ALLOWED_SOURCE_HOSTS`` change needed):
    https://www.stpete.org/government/mayor___city_council/city_council/council_minutes_action_taken.php

Each per-meeting PDF lives at:
    Government/Agendas and Documents/Council Action Taken/{YEAR}/{YYYY-MM-DD} Numbered-Action Taken Agenda.pdf

Live-site recon (2026-08-21) against this exact index page and 10 real
meeting PDFs spanning 2026-01-15 through 2026-08-13 confirmed:

**Roll-call template (rigid, fail-loud on violation).** Every motion
follows ``Roll Call. Ayes. [names]. Nayes. [names]. Absent. [names].``
against the closed 8-person roster (Driscoll, Figgs-Sanders, Floyd,
Gabbard, Givens, Hanewicz, Harting, Gerdes). A free integrity check holds
across all 110 roll-call blocks sampled: Ayes+Nayes+Absent(+Recused) sums
to 8.

**A confirmed 4th vote category beyond DECISIONS #55/56's open
question: "Recused."** The 2026-05-14 PDF contains a roll call reading
``... Nayes. None. Absent. Recused. Harting.`` — i.e. "Recused" appears as
its own category keyword directly inside the Roll Call sentence, the same
shape as Ayes/Nayes/Absent. No "Abstain" was observed anywhere in the
10-PDF sample. VOTE_CATEGORIES below is the confirmed superset
(Ayes/Nayes/Absent/Recused) that ``_parse_roll_call`` recognizes; any other
category keyword inside a Roll Call sentence is unrecognized and fails
loud rather than being silently dropped.

**A second, textually different recusal shape was also found** (2026-04-16,
Councilmember Harting on the FAR-exemption motion): the Roll Call sentence
itself only lists 7 names (no "Recused." category token at all), and the
recusal is instead stated in nearby prose immediately after the sentence:
``(Council Member Harting recused himself.)``. ``_find_prose_recusal``
looks for this pattern in a tight, forward-only window right after the
Roll Call sentence ends (never backward, and never a wide window) to avoid
misattributing a recusal note to the wrong motion. A roll call that still
doesn't sum to 8 after this check fails loud — this is deliberately a
narrow, single confirmed shape, not a general fuzzy fallback.

**pypdf occasionally kerns a stray space into "Figgs-Sanders"** (seen as
"Figgs -Sanders" in the 2026-04-16 PDF's plain-text extraction). Normalized
before roll-call parsing since it's the only hyphenated surname on the
roster and this is a confirmed, narrow live artifact, not a general
whitespace-insensitivity hack.

**Consent-agenda bundling (DECISIONS #56 open item 1) — resolved.**
Checked 4 real meetings with populated Consent Agenda A/B sections
(2026-02-05, 2026-04-16, 2026-06-11, 2026-07-23): in every one, the outline
item "C. Consent Agenda (see attached)" carries exactly ONE roll-call
block that blanket-approves the whole bundle — individual consent line
items (listed later in the same PDF under "Consent Agenda A"/"Consent
Agenda B" headings, each numbered and categorized, e.g. "(Procurement)
1. ...") do NOT get their own roll call. 2026-07-23 additionally confirms
what happens when a member wants to break from the bundle: item 6 of
Consent Agenda B ("CB-6") was pulled via a separate, explicitly-labeled
motion ("Motion to defer agenda item CB-6 to a future meeting") that DOES
get its own roll call, distinct from the blanket vote. This module
reflects that split: ``CouncilAgendaItem`` (one per roll-call block,
includes the blanket Consent Agenda vote as its own item) carries votes;
``CouncilConsentLineItem`` (one per numbered line under Consent Agenda
A/B) carries no vote field, since by default it shares the blanket vote.
A pulled item's separate motion still shows up as its own
``CouncilAgendaItem`` with the "CB-N"/"CA-N" label visible in its
description text — cross-referencing that label to the matching
``CouncilConsentLineItem`` is left to the caller, not done here.

**Archive depth / backfill bound (DECISIONS #56 open item 2) — resolved,
no arbitrary number introduced.** The index page's individually-linked,
one-PDF-per-meeting structure (the shape this module parses) only goes
back to 2026-01-08 — 21 links total, confirmed by enumerating every
`doc-center-entry` on the live page. Everything before that (a "2025
Numbered-Action Take Agendas_Combined.pdf" and equivalent single PDFs for
2020-2024) is a structurally different shape: one PDF bundling an entire
year's numbered agendas together, not one PDF per meeting, format
unverified. ``parse_index`` deliberately recognizes and skips these
"Combined" links (logged, not fail-loud — a known, expected, differently-
shaped link, not a structure break) rather than attempting to feed them
through the per-meeting parser. There is no hardcoded month/year bound in
code because none is needed yet: the crawler naturally stops where the
index page's individual links stop. Reaching further back than
2026-01-08 requires new parsing logic for the "Combined" yearly PDFs and
is out of this build's scope, same flagged-not-followed discipline as
DECISIONS #27.

**Outcome-label capture uses layout-mode PDF extraction, unlike
legistar.py's plain extraction.** ``pypdf``'s ``extraction_mode="layout"``
preserves the left-margin column ("Approved" / "No action" / "Refer to
HLUT" / a resolution number like "2026-53") that plain extraction shoves
to the end of each page, out of reading order. Item segmentation
(assigning each roll-call block to its enclosing outline item, e.g. "H.1")
and outcome-label capture are both best-effort: nullable on a miss, never
fail-loud, since they ride on indentation-depth heuristics (confirmed live:
top-level A-L headings sit at column 21, numbered sub-items at column
25-28) rather than a stable element ID the way Legistar's HTML is. The
roll-call/vote-count contract above is the rigid, fail-loud part; item
labeling and outcome text are corroborating metadata.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.crawlers.base import Attribution, BaseCrawler

logger = logging.getLogger("crawler.stpete_council_votes")

INDEX_URL = (
    "https://www.stpete.org/government/mayor___city_council/city_council/"
    "council_minutes_action_taken.php"
)

DOC_ENTRY_CLASS = "doc-center-entry"
DOC_LINK_CLASS = "doc-file-link"

# The closed 8-person roster, confirmed present (in some combination of the
# 4 vote categories below) on every one of 110 roll-call blocks sampled
# across 10 real meeting PDFs (2026-01-15 through 2026-08-13).
COUNCIL_ROSTER = frozenset(
    {
        "Driscoll",
        "Figgs-Sanders",
        "Floyd",
        "Gabbard",
        "Givens",
        "Hanewicz",
        "Harting",
        "Gerdes",
    }
)

# Confirmed superset (see module docstring): Ayes/Nayes/Absent were the
# only categories DECISIONS #55/56 had verified; live recon this round
# additionally confirmed "Recused" as a real in-sentence category keyword
# (2026-05-14 PDF). No "Abstain" observed. An unrecognized category word
# inside a Roll Call sentence is NOT added here silently - it fails loud
# instead (see _parse_roll_call).
VOTE_CATEGORIES = ("Ayes", "Nayes", "Absent", "Recused")

# Matches the individually-dated per-meeting PDF filename shape, e.g.
# "2026-08-13 Numbered-Action Taken Agenda.pdf" or the hyphen-swapped
# "2026-01-22 Numbered Action-Taken Agenda.pdf" variant (both observed
# live - the site is not internally consistent about hyphen placement).
# Deliberately does NOT match the yearly "..._Combined.pdf" / "All City
# Council Meeting Minutes Combined.pdf" links - those are a different
# document shape (see module docstring, DECISIONS #56 open item 2).
_INDIVIDUAL_MEETING_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+Numbered[\s-]+Action[\s-]*Taken\s+Agenda\.pdf",
    re.IGNORECASE,
)

_ROLL_CALL_START_RE = re.compile(r"Roll\s*Call\.\s*Ayes\.")

_MOVER_SECOND_RE = re.compile(
    r"moved and approved by Councilmember\s+(?P<mover>[A-Za-z][A-Za-z\s-]*?)\s+"
    r"with a second by Councilmember\s+(?P<seconder>[A-Za-z][A-Za-z\s-]*?)\.\s*Roll",
)

# Narrow, confirmed-live prose-recusal shape: "(Council Member Harting
# recused himself.)" immediately after a Roll Call sentence that only
# lists 7 names. Forward-only, tight window - see module docstring for why
# this is deliberately not a wide/bidirectional fuzzy search.
_PROSE_RECUSAL_RE = re.compile(r"Council(?:\s*Member)?\s+(\w+(?:-\w+)?)\s+recused")
_PROSE_RECUSAL_WINDOW_CHARS = 150

_TOP_LEVEL_HEADING_RE = re.compile(r"^([A-L])\.\s+(\S.*)$")
_NUMBERED_SUBITEM_RE = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")

# Requires a date line immediately after the heading (e.g. "Consent Agenda
# B\nJuly 23, 2026") to distinguish a real section heading from the
# boilerplate NOTE paragraph that follows each heading and itself mentions
# both "Consent Agenda A" and "Consent Agenda B" inline (confirmed live,
# 2026-07-23: 6 raw "Consent Agenda [AB]" substring matches, only 2 are
# real headers - the other 4 are the NOTE text explaining the A/B dollar
# threshold, which name both letters in prose).
_CONSENT_SECTION_RE = re.compile(
    r"Consent Agenda ([AB])\s*\n\s*[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4}"
)
_CONSENT_CATEGORY_RE = re.compile(r"^\(([^)]+)\)\s*$")
_CONSENT_ITEM_RE = re.compile(r"^(\d{1,2})\.\s+(.*)$")


@dataclass(frozen=True)
class CouncilVoteBreakdown:
    ayes: tuple[str, ...]
    nayes: tuple[str, ...]
    absent: tuple[str, ...]
    recused: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.ayes) + len(self.nayes) + len(self.absent) + len(self.recused)


def _votes_are_exact_roster(votes: CouncilVoteBreakdown) -> bool:
    """True only if the collected names are exactly the 8-member roster,
    each counted once. `total == 8` alone is insufficient - a name counted
    twice (the same class of pypdf artifact as the confirmed "Figgs
    -Sanders" kerning glitch) can hit total==8 while a different roster
    member is silently missing from the record entirely (crawler-review
    finding, pre-commit review on DECISIONS #56's build)."""
    names = votes.ayes + votes.nayes + votes.absent + votes.recused
    return len(names) == 8 and set(names) == COUNCIL_ROSTER


@dataclass(frozen=True)
class CouncilAgendaItem:
    """One roll-call block from the main lettered/numbered outline (A-L),
    including the single blanket vote for "C. Consent Agenda" as its own
    item - see module docstring on why consent line items don't each get
    one of these."""

    item_label: str | None  # best-effort, e.g. "H.1" or "C"; nullable, see docstring
    description: str | None  # best-effort text preceding the motion; nullable
    mover: str | None
    seconder: str | None
    votes: CouncilVoteBreakdown
    outcome: str | None  # verbatim label, e.g. "Approved", "No action", "Refer to HLUT"
    meeting_date: date
    attribution: Attribution


@dataclass(frozen=True)
class CouncilConsentLineItem:
    """One numbered line under a "Consent Agenda A" / "Consent Agenda B"
    listing. Carries no vote - it shares the blanket CouncilAgendaItem vote
    for "C. Consent Agenda" unless separately pulled and re-moved (which
    shows up as its own CouncilAgendaItem instead, referencing this item's
    "CA-N"/"CB-N" label in its description text - not cross-linked here)."""

    consent_agenda: str  # "A" or "B"
    item_number: int
    category: str | None  # nearest preceding "(Procurement)"-style heading
    description: str
    meeting_date: date
    attribution: Attribution


@dataclass(frozen=True)
class CouncilMeeting:
    meeting_date: date
    pdf_url: str
    agenda_items: tuple[CouncilAgendaItem, ...]
    consent_line_items: tuple[CouncilConsentLineItem, ...]
    attribution: Attribution


def _normalize_plain_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    # Confirmed live pypdf kerning artifact (2026-04-16 PDF) - the only
    # hyphenated roster surname, so this targeted fix can't collide with
    # anything else on the roster.
    text = re.sub(r"Figgs\s*-\s*Sanders", "Figgs-Sanders", text)
    return text


class StpeteCouncilVotesCrawler(BaseCrawler):
    def __init__(self, **kwargs) -> None:
        super().__init__(source_name="stpete_council_votes", **kwargs)

    def crawl(self) -> list[CouncilMeeting]:
        resp = self.fetch(INDEX_URL)
        links = self.parse_index(resp.text)
        meetings = []
        for meeting_date, pdf_url in links:
            pdf_resp = self.fetch(pdf_url)
            meetings.append(self.parse_meeting_pdf(pdf_resp.content, pdf_url, meeting_date))
        return meetings

    # --- Index page -----------------------------------------------------

    def parse_index(self, html: str) -> list[tuple[date, str]]:
        """Returns (meeting_date, absolute_pdf_url) for every individually-
        dated meeting PDF on the index page, newest first (page order).
        "Combined" yearly-archive links are recognized and skipped (logged,
        not fail-loud - see module docstring, DECISIONS #56 open item 2)."""
        soup = BeautifulSoup(html, "lxml")

        entries = soup.find_all("div", class_=DOC_ENTRY_CLASS)
        if not entries:
            self.fail_loud(
                f"no div.{DOC_ENTRY_CLASS} document entries found on {INDEX_URL} "
                "- stpete.org's document-center page structure may have changed"
            )

        base_tag = soup.find("base")
        link_base = base_tag.get("href") if base_tag is not None and base_tag.get("href") else INDEX_URL

        links: list[tuple[date, str]] = []
        skipped_combined = 0
        for entry in entries:
            a = entry.find("a", class_=DOC_LINK_CLASS)
            if a is None or not a.get("href"):
                continue
            href = a.get("href")
            match = _INDIVIDUAL_MEETING_RE.search(href)
            if match is None:
                skipped_combined += 1
                continue
            try:
                meeting_date = datetime.strptime(match.group("date"), "%Y-%m-%d").date()
            except ValueError:
                self.fail_loud(
                    f"could not parse meeting date from filename {href!r} on {INDEX_URL}"
                )
            links.append((meeting_date, urljoin(link_base, href)))

        if not links:
            self.fail_loud(
                f"found {len(entries)} document entries on {INDEX_URL} but none matched "
                "the individually-dated meeting PDF filename pattern - the naming "
                "convention may have changed"
            )

        logger.info(
            "stpete_council_votes: %d individual meeting PDFs found, %d combined-archive "
            "links skipped (out of scope, see DECISIONS #56 open item 2)",
            len(links),
            skipped_combined,
        )
        return links

    # --- Per-meeting PDF --------------------------------------------------

    def parse_meeting_pdf(self, pdf_bytes: bytes, pdf_url: str, meeting_date: date) -> CouncilMeeting:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            plain_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            layout_text = "\n".join(
                page.extract_text(extraction_mode="layout") or "" for page in reader.pages
            )
        except Exception as exc:  # pypdf raises various exceptions on malformed PDFs
            self.fail_loud(f"could not parse PDF at {pdf_url}: {exc}")

        if not plain_text.strip():
            self.fail_loud(f"PDF at {pdf_url} produced no extractable text")

        retrieval_time = datetime.now(timezone.utc)
        page_attribution = Attribution(
            source_url=pdf_url, retrieval_timestamp=retrieval_time, published_date=meeting_date
        )

        outcomes = self._extract_outcomes_by_order(layout_text)
        agenda_items = self._parse_agenda_items(plain_text, meeting_date, pdf_url, outcomes, retrieval_time)
        consent_items = self._parse_consent_line_items(plain_text, meeting_date, pdf_url, retrieval_time)

        return CouncilMeeting(
            meeting_date=meeting_date,
            pdf_url=pdf_url,
            agenda_items=tuple(agenda_items),
            consent_line_items=tuple(consent_items),
            attribution=page_attribution,
        )

    # --- Roll-call parsing (the rigid, fail-loud contract) -----------------

    def _parse_agenda_items(
        self,
        raw_text: str,
        meeting_date: date,
        pdf_url: str,
        outcomes: list[str | None],
        retrieval_time: datetime,
    ) -> list[CouncilAgendaItem]:
        # Only the outline section (before any appended "Consent Agenda A"/
        # "Consent Agenda B" listing) carries roll-call motions - the
        # appended listings are handled separately by
        # _parse_consent_line_items and never carry their own roll call.
        section_end = _CONSENT_SECTION_RE.search(raw_text)
        outline_text = raw_text[: section_end.start()] if section_end else raw_text

        norm = _normalize_plain_text(outline_text)

        items: list[CouncilAgendaItem] = []
        matches = list(_ROLL_CALL_START_RE.finditer(norm))
        if not matches:
            self.fail_loud(
                f"no 'Roll Call. Ayes.' motions found on {pdf_url} - the vote-record "
                "template may have changed"
            )

        # `outcomes` (from layout_text, the full untruncated document) and
        # `matches` (from norm/plain-text, truncated at the consent-agenda
        # boundary) are paired below purely by list index - that's only
        # safe if the two independent scans found the same number of
        # roll-call triggers. They align on every real fixture checked so
        # far, but nothing structurally guarantees it; if a future PDF
        # trips one scan and not the other, every outcome after the
        # mismatch point would silently shift onto the wrong item instead
        # of going null - a shifted-but-present value reads as confidently
        # correct, which is worse than a visible gap (crawler-review
        # finding, pre-commit review on DECISIONS #56's build).
        if len(outcomes) != len(matches):
            self.fail_loud(
                f"outcome-label scan on {pdf_url} found {len(outcomes)} roll-call "
                f"triggers in layout-mode text but the vote-record scan found "
                f"{len(matches)} in plain-text - these must match 1:1 by document "
                "order or outcome labels would silently misalign onto the wrong item"
            )

        for idx, match in enumerate(matches):
            votes, end_pos = self._parse_roll_call(norm, match, pdf_url)

            preceding_text = norm[: match.start()]
            item_label, description = self._nearest_heading(preceding_text)

            mover_match = _MOVER_SECOND_RE.search(
                norm[max(0, match.start() - 400) : match.start() + 20]
            )
            mover = mover_match.group("mover").strip() if mover_match else None
            seconder = mover_match.group("seconder").strip() if mover_match else None

            outcome = outcomes[idx] if idx < len(outcomes) else None

            attribution = Attribution(
                source_url=pdf_url, retrieval_timestamp=retrieval_time, published_date=meeting_date
            )
            items.append(
                CouncilAgendaItem(
                    item_label=item_label,
                    description=description,
                    mover=mover,
                    seconder=seconder,
                    votes=votes,
                    outcome=outcome,
                    meeting_date=meeting_date,
                    attribution=attribution,
                )
            )
        return items

    def _parse_roll_call(
        self, norm_text: str, start_match: re.Match, pdf_url: str
    ) -> tuple[CouncilVoteBreakdown, int]:
        """Walks the token stream after 'Roll Call. Ayes.' assigning each
        name to the current vote category, switching category on any
        recognized VOTE_CATEGORIES keyword. Stops at the first token that's
        neither a category keyword, "None", nor a roster name - that's the
        end of the fixed-template sentence. Fails loud if the resulting set
        of names (plus a confirmed narrow prose-recusal fallback, see
        module docstring) isn't exactly the 8-member roster with no
        repeats, or if the stop token looks like an unrecognized
        vote-category word rather than ordinary prose.

        Checking `total == 8` alone is not enough - a duplicated name (the
        same class of pypdf artifact as the confirmed "Figgs -Sanders"
        kerning glitch, see _normalize_plain_text) can hit total==8 while
        silently masking one dropped roster member entirely from the
        record (crawler-review finding, pre-commit review). Every success
        path below must confirm the collected names are exactly
        COUNCIL_ROSTER, a set with no duplicates, not just 8 of them."""
        pos = start_match.end()
        cur = "Ayes"
        cats: dict[str, list[str]] = {c: [] for c in VOTE_CATEGORIES}
        tokens = norm_text[pos:].split(". ")
        end_pos = pos
        stop_token: str | None = None
        for tok in tokens:
            t = tok.strip().rstrip(".")
            if not t:
                end_pos += len(tok) + 2
                continue
            if t in VOTE_CATEGORIES:
                cur = t
                end_pos += len(tok) + 2
                continue
            if t == "None":
                end_pos += len(tok) + 2
                continue
            if t in COUNCIL_ROSTER:
                cats[cur].append(t)
                end_pos += len(tok) + 2
                continue
            stop_token = t
            break

        votes = CouncilVoteBreakdown(
            ayes=tuple(cats["Ayes"]),
            nayes=tuple(cats["Nayes"]),
            absent=tuple(cats["Absent"]),
            recused=tuple(cats["Recused"]),
        )

        if _votes_are_exact_roster(votes):
            return votes, end_pos

        # Confirmed narrow live shape: a recusal noted in prose immediately
        # after the sentence, member omitted entirely from the roll call
        # (see module docstring). Forward-only, tight window.
        window = norm_text[end_pos : end_pos + _PROSE_RECUSAL_WINDOW_CHARS]
        prose_recused = set(_PROSE_RECUSAL_RE.findall(window)) & COUNCIL_ROSTER
        if votes.total + len(prose_recused) == 8:
            merged = CouncilVoteBreakdown(
                ayes=votes.ayes,
                nayes=votes.nayes,
                absent=votes.absent,
                recused=tuple(sorted(set(votes.recused) | prose_recused)),
            )
            if _votes_are_exact_roster(merged):
                return merged, end_pos
            votes = merged  # carry forward for the error report below

        # Distinguish *why* the names don't form an exact 8-member roster -
        # a duplicate masking a dropped member is a materially different
        # (and more dangerous - it hits total==8) bug than an honest
        # under/over count, so report it distinctly.
        all_names = votes.ayes + votes.nayes + votes.absent + votes.recused
        duplicates = sorted({n for n in all_names if all_names.count(n) > 1})
        missing = sorted(COUNCIL_ROSTER - set(all_names))
        if duplicates:
            self.fail_loud(
                f"roll call on {pdf_url} counted at least one roster member more than "
                f"once ({duplicates}), which masked total==8 while actually dropping "
                f"{missing or '(unknown - recount)'} from the record entirely: {cats}"
            )

        # Flag distinctly if the stop token looks like it was trying to be
        # a new (unrecognized) vote-category word, per DECISIONS #55/56's
        # "fail loud on anything that doesn't match the expected
        # categories" instruction - a short, title-case, single-word-ish
        # token right at the point parsing gave up is the signature of an
        # unrecognized category keyword rather than the sentence just
        # ending into normal prose.
        looks_like_new_category = bool(
            stop_token and re.match(r"^[A-Z][a-z]+$", stop_token) and len(stop_token) < 20
        )
        if looks_like_new_category:
            self.fail_loud(
                f"roll call on {pdf_url} has an unrecognized vote category "
                f"{stop_token!r} (recognized: {VOTE_CATEGORIES}) - counted "
                f"{votes.total}/8: {cats}"
            )
        self.fail_loud(
            f"roll call on {pdf_url} sums to {votes.total}, expected 8 distinct roster "
            f"members (missing: {missing}): {cats} (stop token: {stop_token!r})"
        )

    def _nearest_heading(self, preceding_text: str) -> tuple[str | None, str | None]:
        """Best-effort: the nearest preceding top-level (A-L) heading and,
        if closer, the nearest preceding numbered sub-item, combined into a
        label like "H.1" or "C". Nullable - a miss here doesn't affect the
        roll-call/vote contract, only this corroborating metadata."""
        lines = preceding_text.splitlines()
        top_label: str | None = None
        top_desc: str | None = None
        sub_label: str | None = None
        sub_desc: str | None = None
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if sub_label is None:
                sub_match = _NUMBERED_SUBITEM_RE.match(stripped)
                if sub_match:
                    sub_label, sub_desc = sub_match.group(1), sub_match.group(2)
            top_match = _TOP_LEVEL_HEADING_RE.match(stripped)
            if top_match:
                top_label, top_desc = top_match.group(1), top_match.group(2)
                break

        if top_label is None:
            return None, None
        if sub_label is not None:
            return f"{top_label}.{sub_label}", sub_desc
        return top_label, top_desc

    def _extract_outcomes_by_order(self, layout_text: str) -> list[str | None]:
        """Collects the left-margin annotation (outcome word or resolution
        number) associated with each roll-call block, in document order, by
        walking layout-mode lines and accumulating margin tokens between
        one roll-call sentence and the next. Best-effort/nullable - see
        module docstring on why this rides on layout-column heuristics
        rather than a stable structural anchor."""
        lines = layout_text.split("\n")
        # Margin annotations sit within the first few columns, separated
        # from the main text column by a wide gap (confirmed live: body
        # text starts at column ~21+; a >=10-space gap reliably separates
        # a short margin token from body text without false-splitting
        # justified body lines, which only ever have double-spacing).
        margin_re = re.compile(r"^\s{0,4}(\S.{0,40}?)\s{10,}(.*)$")

        # Split each line into (margin_token, body) up front. A roll-call
        # sentence can wrap onto a second physical line ("...Roll\nCall.
        # Ayes. ..."), so the real trigger check below looks at each body
        # line joined with the next one, requiring "Ayes." to actually
        # follow "Roll Call." - a bare "Roll Call." substring alone isn't
        # enough. Confirmed live false positive this guards against: outline
        # item "A." reads "Meeting Called to Order and Roll Call." (ordinary
        # prose, no vote), which contains "Roll Call." but is never followed
        # by "Ayes." - the naive substring-only check misfired on it.
        parsed_lines: list[tuple[str | None, str]] = []
        for line in lines:
            if not line.strip():
                continue
            m = margin_re.match(line)
            if m:
                parsed_lines.append((m.group(1).strip(), m.group(2)))
            else:
                parsed_lines.append((None, line.strip()))

        outcomes: list[str | None] = []
        pending_margin_tokens: list[str] = []
        triggered_this_block = False
        for i, (margin_token, body) in enumerate(parsed_lines):
            if margin_token:
                pending_margin_tokens.append(margin_token)

            lookahead = body + " " + (parsed_lines[i + 1][1] if i + 1 < len(parsed_lines) else "")
            is_real_trigger = bool(re.search(r"Roll\s*Call\.\s*Ayes\.", lookahead))

            if is_real_trigger and not triggered_this_block:
                outcomes.append(" ".join(pending_margin_tokens).strip() or None)
                pending_margin_tokens = []
                triggered_this_block = True
                continue

            # The roster listing (Ayes/Nayes/Absent[/Recused] names) fits on
            # one physical line in every real block observed - "Absent." is
            # always present (even when nobody's absent: "Absent. None.").
            # Reset right after it so a second motion with no new heading
            # in between (confirmed live: 2026-07-23 has 3 extra motions -
            # reconsider / approve-without-CB-6 / defer-CB-6 - all still
            # under "C. Consent Agenda" with no intervening heading) still
            # gets its own fresh accumulation window, not folded into the
            # next block's tokens. A new heading/sub-item line is kept as a
            # secondary reset in case a roll call is ever split oddly
            # across lines without "Absent." landing on one of them.
            if triggered_this_block and (
                "Absent." in body
                or _TOP_LEVEL_HEADING_RE.match(body)
                or _NUMBERED_SUBITEM_RE.match(body)
            ):
                triggered_this_block = False

        return outcomes

    # --- Consent Agenda A/B line-item listings ------------------------------

    def _parse_consent_line_items(
        self, raw_text: str, meeting_date: date, pdf_url: str, retrieval_time: datetime
    ) -> list[CouncilConsentLineItem]:
        """Parses the appended "Consent Agenda A" / "Consent Agenda B"
        listings into individual line items. These never carry a roll call
        of their own (see module docstring) - resolution numbers are left
        uncaptured this round (the column is scrambled in plain-text
        extraction and reconstructing it reliably is unverified beyond
        DECISIONS #56's explicit ask; flagged in the crawler's report, not
        guessed at here)."""
        items: list[CouncilConsentLineItem] = []
        section_matches = list(_CONSENT_SECTION_RE.finditer(raw_text))
        for i, sec_match in enumerate(section_matches):
            agenda_letter = sec_match.group(1)
            section_start = sec_match.end()
            section_end = (
                section_matches[i + 1].start() if i + 1 < len(section_matches) else len(raw_text)
            )
            section_text = raw_text[section_start:section_end]

            current_category: str | None = None
            lines = section_text.splitlines()
            idx = 0
            while idx < len(lines):
                line = lines[idx].strip()
                if not line:
                    idx += 1
                    continue
                cat_match = _CONSENT_CATEGORY_RE.match(line)
                if cat_match:
                    current_category = cat_match.group(1)
                    idx += 1
                    continue
                item_match = _CONSENT_ITEM_RE.match(line)
                if item_match:
                    item_number = int(item_match.group(1))
                    desc_lines = [item_match.group(2)]
                    idx += 1
                    # A description can wrap multiple lines; keep consuming
                    # until the next numbered item, category heading, or
                    # blank-line-delimited boundary.
                    while idx < len(lines):
                        nxt = lines[idx].strip()
                        if not nxt:
                            break
                        if _CONSENT_ITEM_RE.match(nxt) or _CONSENT_CATEGORY_RE.match(nxt):
                            break
                        desc_lines.append(nxt)
                        idx += 1
                    description = " ".join(desc_lines).strip()
                    attribution = Attribution(
                        source_url=pdf_url,
                        retrieval_timestamp=retrieval_time,
                        published_date=meeting_date,
                    )
                    items.append(
                        CouncilConsentLineItem(
                            consent_agenda=agenda_letter,
                            item_number=item_number,
                            category=current_category,
                            description=description,
                            meeting_date=meeting_date,
                            attribution=attribution,
                        )
                    )
                    continue
                idx += 1
        return items
