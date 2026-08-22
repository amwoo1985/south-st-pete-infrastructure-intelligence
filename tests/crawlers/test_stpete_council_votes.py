"""Tests for app/crawlers/stpete_council_votes.py.

Uses real recorded fixtures (tests/fixtures/stpete_council_votes/) captured
from www.stpete.org's "Council Minutes & Action Taken" index page and 5 of
its per-meeting PDFs during this session's live recon (2026-08-21) — see
DECISIONS #56 and the module's own docstring for what each fixture was
chosen to demonstrate. Structure-failure (fail-loud) cases use small
hand-built synthetic text, matching test_legistar.py's pattern, since
there's no live "broken" page/PDF to record.

Fixture-to-finding map:
- council_minutes_index.html: the live index page, 21 individual meeting
  links + 12 combined-archive links correctly told apart (DECISIONS #56
  open item 2 - archive-depth bound).
- 2026-08-13_meeting.pdf: DECISIONS #55's own reference case - confirms
  Givens as the lone Nay on two ordinances and Floyd on a third.
- 2026-02-05_meeting.pdf: a populated Consent Agenda A/B listing with the
  ordinary case - one blanket roll call for "C. Consent Agenda", no
  per-line-item votes (DECISIONS #56 open item 1). Also the cleanest
  outcome-label sample (Approved / No action / Refer to HLUT).
- 2026-07-23_meeting.pdf: the consent-agenda-pull case - item "CB-6" gets
  pulled from the blanket vote via its own separately-moved, separately
  labeled motion (DECISIONS #56 open item 1's edge case).
- 2026-05-14_meeting.pdf: a real "Recused" vote category appearing inside
  the Roll Call sentence itself (the vote-vocabulary superset finding).
- 2026-04-16_meeting.pdf: a real recusal noted only in prose immediately
  after a 7-name Roll Call sentence, plus the "Figgs -Sanders" stray-space
  kerning artifact.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
import responses

from app.crawlers.base import CrawlerStructureError
from app.crawlers.stpete_council_votes import (
    COUNCIL_ROSTER,
    INDEX_URL,
    StpeteCouncilVotesCrawler,
    _normalize_plain_text,
    _ROLL_CALL_START_RE,
)
from tests.conftest import register_robots_permissive

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "stpete_council_votes"


def load_fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def make_crawler() -> StpeteCouncilVotesCrawler:
    return StpeteCouncilVotesCrawler(min_request_interval_seconds=0)


# --- Fail-loud: index page structure -----------------------------------


def test_parse_index_missing_entries_raises():
    html = "<html><body><p>stpete.org redesigned this page</p></body></html>"
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="no div.doc-center-entry"):
        crawler.parse_index(html)


def test_parse_index_entries_but_no_individual_pdfs_raises():
    """Entries exist, but none match the individually-dated meeting PDF
    filename pattern - e.g. the site renamed the naming convention."""
    html = """
    <html><body>
    <div class="doc-center-entry">
      <a class="doc-file-link" href="Government/Agendas and Documents/Council Action Taken/2026 Combined.pdf">2026 Combined</a>
    </div>
    </body></html>
    """
    crawler = make_crawler()
    with pytest.raises(CrawlerStructureError, match="naming convention may have changed"):
        crawler.parse_index(html)


# --- Real-fixture parsing: the index page -------------------------------


def test_parse_index_real_fixture_separates_individual_from_combined():
    html = load_fixture_text("council_minutes_index.html")
    crawler = make_crawler()
    links = crawler.parse_index(html)

    # Live recon 2026-08-21: exactly 21 individually-dated meeting PDFs on
    # the page (2026-01-08 through 2026-08-13); the pre-2026 archive is
    # only available as differently-shaped yearly "Combined" PDFs, which
    # must NOT show up here (DECISIONS #56 open item 2).
    assert len(links) == 21
    dates = {d for d, _ in links}
    assert date(2026, 8, 13) in dates
    assert date(2026, 1, 8) in dates
    assert min(dates) == date(2026, 1, 8)
    assert max(dates) == date(2026, 8, 13)

    for meeting_date, pdf_url in links:
        assert pdf_url.startswith("https://www.stpete.org/")
        assert "Combined" not in pdf_url


# --- _parse_roll_call: the rigid, fail-loud vote-count contract ---------


def test_parse_roll_call_full_ayes_sums_to_8():
    crawler = make_crawler()
    text = (
        "Roll Call. Ayes. Driscoll. Figgs-Sanders. Floyd. Gabbard. Gerdes. "
        "Givens. Hanewicz. Harting. Nayes. None. Absent. None. Next sentence."
    )
    match = _ROLL_CALL_START_RE.search(text)
    votes, end_pos = crawler._parse_roll_call(text, match, "https://example/x.pdf")
    assert len(votes.ayes) == 8
    assert votes.total == 8


def test_parse_roll_call_recused_category_recognized():
    """Live-confirmed shape (2026-05-14): "Recused" appears as its own
    category keyword directly inside the sentence."""
    crawler = make_crawler()
    text = (
        "Roll Call. Ayes. Driscoll. Figgs-Sanders. Floyd. Gabbard. Gerdes. "
        "Givens. Hanewicz. Nayes. None. Absent. Recused. Harting. Next sentence."
    )
    match = _ROLL_CALL_START_RE.search(text)
    votes, end_pos = crawler._parse_roll_call(text, match, "https://example/x.pdf")
    assert votes.recused == ("Harting",)
    assert votes.total == 8


def test_parse_roll_call_prose_recusal_fallback():
    """Live-confirmed shape (2026-04-16): only 7 names in the sentence
    itself, with the 8th member's recusal stated in prose right after."""
    crawler = make_crawler()
    text = (
        "Roll Call. Ayes. Driscoll. Figgs-Sanders. Gabbard. Gerdes. Givens. "
        "Hanewicz. Nayes. None. Absent. Floyd. (Council Member Harting recused himself.) "
        "Next sentence starts here."
    )
    match = _ROLL_CALL_START_RE.search(text)
    votes, end_pos = crawler._parse_roll_call(text, match, "https://example/x.pdf")
    assert votes.recused == ("Harting",)
    assert votes.total == 8


def test_parse_roll_call_mismatch_without_explanation_fails_loud():
    """A genuine 7-count with no recognizable recusal note nearby - the
    fail-loud condition DECISIONS #55/56 require, not a silent drop."""
    crawler = make_crawler()
    text = (
        "Roll Call. Ayes. Driscoll. Figgs-Sanders. Floyd. Gabbard. Gerdes. "
        "Givens. Hanewicz. Nayes. None. Absent. None. Next sentence with no recusal note."
    )
    match = _ROLL_CALL_START_RE.search(text)
    with pytest.raises(CrawlerStructureError, match="sums to 7, expected 8"):
        crawler._parse_roll_call(text, match, "https://example/x.pdf")


def test_parse_roll_call_duplicate_name_masking_dropped_member_fails_loud():
    """crawler-review finding (pre-commit adversarial pass): `total == 8`
    alone isn't a sufficient integrity check - a duplicated name can hit
    total==8 while a different roster member is dropped entirely and
    neither prior fail-loud branch would catch it. Driscoll counted twice,
    Harting never appears anywhere in the sentence."""
    crawler = make_crawler()
    text = (
        "Roll Call. Ayes. Driscoll. Driscoll. Figgs-Sanders. Floyd. Gabbard. Gerdes. "
        "Givens. Hanewicz. Nayes. None. Absent. None. Next sentence."
    )
    match = _ROLL_CALL_START_RE.search(text)
    with pytest.raises(CrawlerStructureError, match="counted at least one roster member more than once"):
        crawler._parse_roll_call(text, match, "https://example/x.pdf")


def test_parse_roll_call_unrecognized_category_fails_loud():
    """A vote category beyond the confirmed superset (Ayes/Nayes/Absent/
    Recused) - e.g. "Abstain", never observed live - must fail loud rather
    than being silently absorbed or dropped (DECISIONS #55/56)."""
    crawler = make_crawler()
    text = (
        "Roll Call. Ayes. Driscoll. Figgs-Sanders. Floyd. Gabbard. Gerdes. "
        "Givens. Hanewicz. Nayes. None. Absent. None. Abstain. Harting. Next sentence."
    )
    match = _ROLL_CALL_START_RE.search(text)
    with pytest.raises(CrawlerStructureError, match="unrecognized vote category 'Abstain'"):
        crawler._parse_roll_call(text, match, "https://example/x.pdf")


def test_normalize_plain_text_fixes_kerned_figgs_sanders():
    """Confirmed live pypdf artifact (2026-04-16 fixture): a stray space
    kerned into the only hyphenated roster surname."""
    assert "Figgs -Sanders" not in _normalize_plain_text("Ayes. Figgs -Sanders. Floyd.")
    assert "Figgs-Sanders" in _normalize_plain_text("Ayes. Figgs -Sanders. Floyd.")


# --- _parse_agenda_items: outcomes/matches alignment (crawler-review) ---


def test_parse_agenda_items_outcome_length_mismatch_fails_loud():
    """crawler-review finding (pre-commit adversarial pass): `outcomes`
    (built from layout_text) and `matches` (built from plain-text) are
    paired purely by list index. They align on every real fixture checked,
    but nothing structurally guarantees it - if the two independent scans
    ever disagree on how many roll-call triggers exist, this must fail
    loud rather than silently shifting every outcome after the mismatch
    point onto the wrong item."""
    crawler = make_crawler()
    raw_text = (
        "A.     Heading one.\n"
        "A motion was moved and approved by Councilmember Driscoll with a second by "
        "Councilmember Floyd. Roll Call. Ayes. Driscoll. Figgs-Sanders. Floyd. Gabbard. "
        "Gerdes. Givens. Hanewicz. Harting. Nayes. None. Absent. None.\n"
        "B.     Heading two.\n"
        "A motion was moved and approved by Councilmember Gerdes with a second by "
        "Councilmember Givens. Roll Call. Ayes. Driscoll. Figgs-Sanders. Floyd. Gabbard. "
        "Gerdes. Givens. Hanewicz. Harting. Nayes. None. Absent. None.\n"
    )
    # Two real roll-call blocks above, but only one outcome supplied -
    # simulates the layout-mode scan finding a different trigger count
    # than the plain-text scan.
    with pytest.raises(CrawlerStructureError, match="found 1 roll-call triggers .* found 2"):
        crawler._parse_agenda_items(
            raw_text, date(2026, 1, 1), "https://example/x.pdf", [None], datetime.now()
        )


# --- Real-fixture parsing: full meeting PDFs ----------------------------


def test_parse_meeting_pdf_2026_08_13_matches_known_real_dissents():
    """DECISIONS #55's own verified real-world case: Givens the lone Nay
    on Ordinance 644-H and 646-H, Floyd the lone Nay on 645-H."""
    pdf_bytes = load_fixture_bytes("2026-08-13_meeting.pdf")
    crawler = make_crawler()
    meeting = crawler.parse_meeting_pdf(
        pdf_bytes, "https://www.stpete.org/2026-08-13.pdf", date(2026, 8, 13)
    )

    assert meeting.meeting_date == date(2026, 8, 13)
    assert len(meeting.agenda_items) > 0

    dissent_items = [it for it in meeting.agenda_items if it.votes.nayes]
    givens_dissents = [it for it in dissent_items if it.votes.nayes == ("Givens",)]
    floyd_dissents = [it for it in dissent_items if it.votes.nayes == ("Floyd",)]
    assert len(givens_dissents) == 2
    assert len(floyd_dissents) == 1

    for item in meeting.agenda_items:
        assert item.votes.total == 8
        # Mandatory attribution per .claude/rules/crawler.md.
        assert item.attribution.source_url
        assert item.attribution.published_date == date(2026, 8, 13)
        assert isinstance(item.attribution.retrieval_timestamp, datetime)
        assert item.attribution.retrieval_timestamp.tzinfo is not None
        for name in item.votes.ayes + item.votes.nayes + item.votes.absent + item.votes.recused:
            assert name in COUNCIL_ROSTER


def test_parse_meeting_pdf_2026_02_05_consent_agenda_gets_one_blanket_vote():
    """DECISIONS #56 open item 1, the ordinary case: "C. Consent Agenda"
    gets exactly one roll-call block, and the individual line items listed
    later in the same PDF under Consent Agenda A/B carry no vote."""
    pdf_bytes = load_fixture_bytes("2026-02-05_meeting.pdf")
    crawler = make_crawler()
    meeting = crawler.parse_meeting_pdf(
        pdf_bytes, "https://www.stpete.org/2026-02-05.pdf", date(2026, 2, 5)
    )

    consent_votes = [it for it in meeting.agenda_items if it.item_label == "C"]
    assert len(consent_votes) == 1
    assert consent_votes[0].votes.total == 8

    assert len(meeting.consent_line_items) == 11
    for line_item in meeting.consent_line_items:
        assert line_item.consent_agenda in ("A", "B")
        assert line_item.description
        assert line_item.attribution.published_date == date(2026, 2, 5)

    # Outcome-label capture (layout-margin best-effort field).
    outcomes = {it.outcome for it in meeting.agenda_items}
    assert "Approved" in outcomes
    assert any(o and "Refer to" in o and "HLUT" in o for o in outcomes if o)


def test_parse_meeting_pdf_2026_07_23_consent_pull_gets_its_own_vote():
    """DECISIONS #56 open item 1's edge case: item 6 of Consent Agenda B
    ("CB-6") is pulled from the blanket vote and separately moved/voted on
    - confirmed live by the presence of 4 distinct "C"-labeled roll calls
    (blanket approve, reconsider, approve-without-CB-6, defer-CB-6) instead
    of just 1."""
    pdf_bytes = load_fixture_bytes("2026-07-23_meeting.pdf")
    crawler = make_crawler()
    meeting = crawler.parse_meeting_pdf(
        pdf_bytes, "https://www.stpete.org/2026-07-23.pdf", date(2026, 7, 23)
    )

    consent_related_votes = [it for it in meeting.agenda_items if it.item_label == "C"]
    assert len(consent_related_votes) == 4
    for it in consent_related_votes:
        assert it.votes.total == 8

    cb6 = next(
        c for c in meeting.consent_line_items if c.consent_agenda == "B" and c.item_number == 6
    )
    assert cb6.category == "Procurement"
    assert cb6.description


def test_parse_meeting_pdf_2026_05_14_real_recused_category():
    pdf_bytes = load_fixture_bytes("2026-05-14_meeting.pdf")
    crawler = make_crawler()
    meeting = crawler.parse_meeting_pdf(
        pdf_bytes, "https://www.stpete.org/2026-05-14.pdf", date(2026, 5, 14)
    )
    recused_items = [it for it in meeting.agenda_items if it.votes.recused]
    assert len(recused_items) == 1
    assert recused_items[0].votes.recused == ("Harting",)
    assert recused_items[0].votes.total == 8


def test_parse_meeting_pdf_2026_04_16_prose_recusal_and_kerning_fix():
    pdf_bytes = load_fixture_bytes("2026-04-16_meeting.pdf")
    crawler = make_crawler()
    meeting = crawler.parse_meeting_pdf(
        pdf_bytes, "https://www.stpete.org/2026-04-16.pdf", date(2026, 4, 16)
    )
    recused_items = [it for it in meeting.agenda_items if it.votes.recused]
    assert len(recused_items) >= 1
    assert recused_items[0].votes.recused == ("Harting",)
    assert recused_items[0].votes.total == 8
    # The kerning-normalized name must appear cleanly in the roster tuples,
    # never as "Figgs -Sanders".
    for it in meeting.agenda_items:
        assert "Figgs -Sanders" not in (it.votes.ayes + it.votes.nayes + it.votes.absent)


# --- crawl() end-to-end against mocked HTTP -----------------------------


@responses.activate
def test_crawl_fetches_index_and_one_pdf():
    register_robots_permissive(responses, host="www.stpete.org")
    responses.add(
        responses.GET, INDEX_URL, body=load_fixture_text("council_minutes_index.html"), status=200
    )
    pdf_url = (
        "https://www.stpete.org/Government/Agendas and Documents/Council Action Taken/"
        "2026/2026-08-13 Numbered-Action Taken Agenda.pdf?t=202608161112020"
    )
    responses.add(
        responses.GET,
        pdf_url,
        body=load_fixture_bytes("2026-08-13_meeting.pdf"),
        status=200,
        content_type="application/pdf",
    )
    # Every other individual-meeting PDF also needs a mock, or `responses`
    # raises a ConnectionError - crawl() fetches all 21 links it finds.
    for _, url in make_crawler().parse_index(load_fixture_text("council_minutes_index.html")):
        if url != pdf_url:
            responses.add(
                responses.GET,
                url,
                body=load_fixture_bytes("2026-08-13_meeting.pdf"),
                status=200,
                content_type="application/pdf",
            )

    crawler = make_crawler()
    meetings = crawler.crawl()

    assert len(meetings) == 21
    aug13 = next(m for m in meetings if m.meeting_date == date(2026, 8, 13))
    assert len(aug13.agenda_items) > 0
