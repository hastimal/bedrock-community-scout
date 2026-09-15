"""
Unit tests for live Meetup event-date handling in AgentCoreWebSearchEventSource.

These cover the real AgentCore Web Search evidence formats discovered during live
integration:

  * ``June 24, 2026`` (full English month, date only)
  * ``Thu, Sep 17 · 6:00 PM CDT`` (Meetup abbreviated format, NO year)
  * ``Wed, Oct 28 · 6:00 PM CDT``

and the case where a single Meetup group-page result contains MULTIPLE event dates,
some outside and some inside the requested window.

Rules enforced (evidence-only factual accuracy):
  * The event date is taken from the evidence text, never from ``publishedDate``.
  * Meetup's yearless format resolves its year deterministically from the requested
    window; the year is never invented.
  * On a multi-event group page, the selected date must fall inside the window — the
    first date is not blindly taken.
  * If a date cannot be reliably associated/placed in-window, the record is excluded.
"""

from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


MEETUP_URL = "https://www.meetup.com/austin-enterprise-ai/events/301/"


def _result(text):
    """A minimal real-shaped Web Search result dict carrying evidence in 'text'."""
    return {
        "title": "Enterprise AI & Engineering",
        "url": MEETUP_URL,
        "text": text,
        # publishedDate deliberately differs from any event date; must be ignored.
        "publishedDate": "2025-01-05",
    }


def _extract(text, start_date, end_date):
    source = AgentCoreWebSearchEventSource()
    return source._extract_raw_event(_result(text), start_date, end_date)


# --------------------------------------------------------------------------- #
# Real single-date formats
# --------------------------------------------------------------------------- #
def test_full_month_date_only_june_24_2026():
    """'June 24, 2026' is extracted and normalized to midnight UTC."""
    raw = _extract(
        "Austin, Texas. Enterprise AI & Engineering meets on June 24, 2026.",
        "2026-01-01",
        "2026-12-31",
    )
    assert raw is not None
    assert raw["event_url"] == MEETUP_URL
    assert raw["city"] == "Austin, Texas"
    assert raw["start_datetime"] == "June 24, 2026"  # normalizer converts downstream


def test_meetup_abbreviated_thu_sep_17():
    """'Thu, Sep 17 · 6:00 PM CDT' resolves its year from the window (2026)."""
    raw = _extract(
        "Austin, Texas · Thu, Sep 17 · 6:00 PM CDT · Agentic AI meetup.",
        "2026-01-01",
        "2026-12-31",
    )
    assert raw is not None
    assert raw["start_datetime"] == "September 17, 2026 at 6:00 PM"


def test_meetup_abbreviated_wed_oct_28():
    """'Wed, Oct 28 · 6:00 PM CDT' resolves its year from the window (2026)."""
    raw = _extract(
        "Austin, Texas Wed, Oct 28 · 6:00 PM CDT AWS deep dive.",
        "2026-01-01",
        "2026-12-31",
    )
    assert raw is not None
    assert raw["start_datetime"] == "October 28, 2026 at 6:00 PM"


# --------------------------------------------------------------------------- #
# Multiple dates on a single group-page result
# --------------------------------------------------------------------------- #
GROUP_PAGE_TEXT = (
    "Austin, Texas. Enterprise AI & Engineering group. Upcoming events: "
    "Thu, Sep 17 · 6:00 PM CDT (kickoff) and Wed, Oct 28 · 6:00 PM CDT (deep dive)."
)


def test_multiple_dates_selects_the_one_inside_october_window():
    """With two dates present, the October window selects Oct 28 (not the first, Sep 17)."""
    raw = _extract(GROUP_PAGE_TEXT, "2026-10-01", "2026-10-31")
    assert raw is not None
    assert raw["start_datetime"] == "October 28, 2026 at 6:00 PM"


def test_multiple_dates_selects_the_one_inside_september_window():
    """With two dates present, the September window selects Sep 17."""
    raw = _extract(GROUP_PAGE_TEXT, "2026-09-01", "2026-09-30")
    assert raw is not None
    assert raw["start_datetime"] == "September 17, 2026 at 6:00 PM"


def test_multiple_dates_none_in_window_excludes_record():
    """When neither date falls in the window, the record is excluded (not fabricated)."""
    raw = _extract(GROUP_PAGE_TEXT, "2026-11-01", "2026-11-30")
    assert raw is None


# --------------------------------------------------------------------------- #
# Year is never invented; publishedDate is never the event date
# --------------------------------------------------------------------------- #
def test_yearless_date_outside_window_is_excluded():
    """A yearless Meetup date that can't be placed in the window is excluded."""
    raw = _extract(
        "Austin, Texas Thu, Sep 17 · 6:00 PM CDT",
        "2026-10-01",
        "2026-10-31",
    )
    assert raw is None


def test_yearless_date_ambiguous_two_year_window_is_excluded():
    """If two years in the window both fit the month/day, the year is ambiguous -> exclude."""
    raw = _extract(
        "Austin, Texas Wed, Oct 28 · 6:00 PM CDT",
        "2025-01-01",
        "2026-12-31",
    )
    assert raw is None


def test_published_date_is_not_used_as_event_date():
    """publishedDate must never supply the event date; no in-text date -> exclude."""
    raw = _extract(
        "Austin, Texas Enterprise AI & Engineering group page. (no event date in text)",
        "2025-01-01",
        "2026-12-31",
    )
    assert raw is None


# --------------------------------------------------------------------------- #
# Year-bearing multiple dates: an explicit year must never be overridden
# --------------------------------------------------------------------------- #
def test_year_bearing_multiple_dates_selects_in_window_without_fabricating_year():
    """Two explicit-year dates: the out-of-window one is skipped and the in-window one
    is chosen. The explicit year in the evidence must never be replaced by a
    window-resolved year (regression: a yearless sub-match must not shadow a long date).
    """
    text = (
        "Austin, Texas. Enterprise AI & Engineering: June 24, 2025 (past) and "
        "October 28, 2026 (upcoming)."
    )
    raw = _extract(text, "2026-01-01", "2026-12-31")
    assert raw is not None
    # The in-window, explicit-year date is selected verbatim (no fabricated year).
    assert raw["start_datetime"] == "October 28, 2026"


def test_year_bearing_date_out_of_window_is_excluded_not_reyeared():
    """A single explicit-year date outside the window is excluded, never re-yeared to fit."""
    raw = _extract(
        "Austin, Texas. Enterprise AI & Engineering on June 24, 2025.",
        "2026-01-01",
        "2026-12-31",
    )
    assert raw is None


# --------------------------------------------------------------------------- #
# Explicit-year Meetup dates: the evidence year is authoritative
# --------------------------------------------------------------------------- #
# A yearless Meetup matcher must NEVER reinterpret a month/day that is part of an
# explicit year-bearing date using the requested window's year.
_EY_WINDOW = ("2026-09-15", "2026-12-14")


def test_explicit_year_meetup_date_out_of_window_is_excluded():
    """'Thu, Nov 20, 2025 · 6:00 PM CST' resolves to 2025-11-20 -> excluded (not 2026)."""
    raw = _extract(
        "Austin, Texas. Past events - Modern Virtualization with Kubevirt and "
        "KubeCon Roundtable discussion Thu, Nov 20, 2025 · 6:00 PM CST",
        *_EY_WINDOW,
    )
    assert raw is None


def test_yearless_meetup_date_still_resolves_from_window():
    """Genuinely yearless 'Wed, Oct 7 · 6:30 PM CDT' resolves to 2026-10-07 in-window."""
    raw = _extract(
        "Austin, Texas. Custom GPTs workshop Wed, Oct 7 · 6:30 PM CDT",
        *_EY_WINDOW,
    )
    assert raw is not None
    assert raw["start_datetime"] == "October 7, 2026 at 6:30 PM"


def test_explicit_year_out_and_yearless_in_selects_the_yearless_in_window_event():
    """Explicit-year-out (Nov 20, 2025) coexists with yearless-in (Oct 7); pick Oct 7 2026."""
    raw = _extract(
        "Austin, Texas. Past: KubeCon Roundtable Thu, Nov 20, 2025 · 6:00 PM CST "
        "Upcoming: How to Build Custom GPTs Wed, Oct 7 · 6:30 PM CDT",
        *_EY_WINDOW,
    )
    assert raw is not None
    assert raw["start_datetime"] == "October 7, 2026 at 6:30 PM"
    # Title associated with the in-window event, not the past one.
    assert "Custom GPTs" in raw["title"]


def test_explicit_year_meetup_date_in_window_is_kept_with_its_year():
    """An explicit-year Meetup date inside the window keeps its authoritative year."""
    raw = _extract(
        "Austin, Texas. Custom GPTs Wed, Oct 7, 2026 · 6:30 PM CDT",
        *_EY_WINDOW,
    )
    assert raw is not None
    assert raw["start_datetime"] == "October 7, 2026 at 6:30 PM"


def test_explicit_year_without_weekday_prefix_is_authoritative():
    """'Nov 20, 2025 · 6:00 PM CST' (no weekday) resolves to 2025 and is excluded."""
    raw = _extract(
        "Austin, Texas. Roundtable Nov 20, 2025 · 6:00 PM CST",
        *_EY_WINDOW,
    )
    assert raw is None
