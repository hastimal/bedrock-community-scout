"""
Regression tests for event-title association on multi-event Meetup group pages.

A Web Search result for a Meetup GROUP page carries the group/page title as the
outer result ``title`` and lists several embedded events in the evidence text, each
with its own title and date. The extractor must associate the selected in-window date
with the event title immediately adjacent to that date — never with the outer
group-page title — and must preserve the original Web Search URL byte-for-byte without
fabricating an event-specific URL.

The primary test uses the exact live pattern discovered during real AgentCore Web
Search integration.
"""

from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource
from src.tools.normalizer import _convert_to_iso8601_utc


# The live result returned the group page URL only (no event-specific URL).
GROUP_URL = "https://www.meetup.com/austin-digital-marketing-for-local-business/"
OUTER_GROUP_TITLE = "AIAM — AI Automation and Marketing"

# Evidence with three embedded events; only the middle one is inside the window.
GROUP_PAGE_EVIDENCE = (
    "AIAM — AI Automation and Marketing. Austin, Texas. Upcoming events: "
    "Hermes Agent: The Personal AI OS for Work and Business "
    "Wed, Aug 5 · 6:30 PM CDT "
    "How to Build and Use Custom GPTs for Real Business Tasks "
    "Wed, Oct 7 · 6:30 PM CDT "
    "Lovable Demo: Turn Pain Points into Prototypes "
    "Wed, Jul 1 · 6:30 PM CDT"
)

# Requested window: 2026-09-15 through 2026-12-14 (only Oct 7 falls inside).
WINDOW_START = "2026-09-15"
WINDOW_END = "2026-12-14"


def _extract():
    source = AgentCoreWebSearchEventSource()
    result = {
        "title": OUTER_GROUP_TITLE,
        "url": GROUP_URL,
        "text": GROUP_PAGE_EVIDENCE,
        # publishedDate deliberately differs from any event date; must be ignored.
        "publishedDate": "2025-01-01",
    }
    return source._extract_raw_event(result, WINDOW_START, WINDOW_END)


def test_selected_date_pairs_with_adjacent_event_title_not_group_title():
    """The Oct 7 date pairs with its adjacent embedded event title, not the group title."""
    raw = _extract()

    assert raw is not None
    assert raw["title"] == "How to Build and Use Custom GPTs for Real Business Tasks"
    # Never the outer Meetup group/page title.
    assert raw["title"] != OUTER_GROUP_TITLE


def test_selected_event_date_is_october_7_2026():
    """The chosen date is the in-window embedded event date (2026-10-07)."""
    raw = _extract()

    assert raw is not None
    assert _convert_to_iso8601_utc(raw["start_datetime"])[:10] == "2026-10-07"


def test_original_group_url_preserved_byte_for_byte_no_fabrication():
    """The original Web Search (group) URL is preserved exactly; no event URL invented."""
    raw = _extract()

    assert raw is not None
    assert raw["event_url"] == GROUP_URL


def test_out_of_window_embedded_events_are_not_selected():
    """The Aug 5 and Jul 1 embedded events (out of window) are never selected."""
    raw = _extract()

    assert raw is not None
    iso_date = _convert_to_iso8601_utc(raw["start_datetime"])[:10]
    assert iso_date != "2026-08-05"
    assert iso_date != "2026-07-01"
    # And the selected title is not either out-of-window event's title.
    assert raw["title"] != "Hermes Agent: The Personal AI OS for Work and Business"
    assert raw["title"] != "Lovable Demo: Turn Pain Points into Prototypes"
