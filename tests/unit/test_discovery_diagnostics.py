"""
Tests for TEMPORARY discovery diagnostic logging in AgentCoreWebSearchEventSource.

These assert the presence and shape of the ``[DISCOVERY_DIAGNOSTIC]`` INFO logs added to
diagnose why Luma events may not be surfacing. The logging is diagnostic-only: these
tests also confirm that the events RETURNED by discovery are unchanged, and that no
credential/header/token values are ever logged.
"""

import logging

import pytest

from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


WINDOW = ("2026-09-15", "2026-12-14")
LOGGER_NAME = "src.tools.agentcore_web_search_event_source"


def _result(title, url, date="2026-10-07"):
    return {"title": title, "url": url, "text": f"Austin, Texas on {date}T18:30:00Z about {title}."}


def _source(meetup_results, luma_results):
    source = AgentCoreWebSearchEventSource()

    def per_source(topics, start, end, src):
        if src == "meetup.com":
            return list(meetup_results)
        if src == "lu.ma":
            return list(luma_results)
        return []

    source._invoke_web_search_for_source = per_source  # type: ignore[assignment]
    return source


def _diag_lines(caplog):
    return [r.message for r in caplog.records if "[DISCOVERY_DIAGNOSTIC]" in r.message]


# --------------------------------------------------------------------------- #
# Meetup + Luma per-source call logging (query, domain filter, raw count)
# --------------------------------------------------------------------------- #
def test_meetup_diagnostic_logging(caplog):
    source = _source(
        meetup_results=[_result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/")],
        luma_results=[],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    # A per-source call line for meetup with the query, domain filter, and raw count.
    meetup_call = [
        m for m in lines
        if "source='meetup'" in m and "query=" in m and "domain_filter=" in m
    ]
    assert meetup_call, "expected a meetup per-source diagnostic call line"
    assert "domain_filter=['meetup.com']" in meetup_call[0]
    assert "raw_results=1" in meetup_call[0]
    assert "site:meetup.com" in meetup_call[0]


def test_luma_diagnostic_logging(caplog):
    source = _source(
        meetup_results=[],
        luma_results=[_result("AWS Bedrock Workshop", "https://lu.ma/austin-bedrock")],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    luma_call = [
        m for m in lines
        if "source='luma'" in m and "query=" in m and "domain_filter=" in m
    ]
    assert luma_call, "expected a luma per-source diagnostic call line"
    assert "domain_filter=['lu.ma']" in luma_call[0]
    assert "raw_results=1" in luma_call[0]
    assert "site:lu.ma" in luma_call[0]


# --------------------------------------------------------------------------- #
# Raw result count + per-raw-result logging (title, url, evidence)
# --------------------------------------------------------------------------- #
def test_raw_result_count_and_per_result_logging(caplog):
    source = _source(
        meetup_results=[
            _result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/"),
            _result("AWS Serverless", "https://www.meetup.com/a/2/"),
        ],
        luma_results=[],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    # Raw count of 2 for meetup.
    assert any("source='meetup'" in m and "raw_results=2" in m and "query=" in m for m in lines)
    # Each raw result logged with index, title, exact url, and evidence.
    raw_lines = [m for m in lines if "result=" in m and "evidence=" in m]
    assert any("result=1" in m and "https://www.meetup.com/a/1/" in m for m in raw_lines)
    assert any("result=2" in m and "https://www.meetup.com/a/2/" in m for m in raw_lines)


# --------------------------------------------------------------------------- #
# Accepted event logging
# --------------------------------------------------------------------------- #
def test_accepted_event_logging(caplog):
    source = _source(
        meetup_results=[_result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/")],
        luma_results=[],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    accepted = [m for m in lines if "status='accepted'" in m]
    assert accepted, "expected an accepted diagnostic line"
    assert any(
        "AWS Cloud Deep Dive" in m and "https://www.meetup.com/a/1/" in m for m in accepted
    )


# --------------------------------------------------------------------------- #
# Rejected event + reason logging
# --------------------------------------------------------------------------- #
def test_rejected_event_topic_irrelevant_reason(caplog):
    source = _source(
        meetup_results=[_result("Hong Kong Mahjong", "https://www.meetup.com/a/9/")],
        luma_results=[],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    rejected = [m for m in lines if "status='rejected'" in m]
    assert rejected, "expected a rejected diagnostic line"
    assert any(
        "reason='topic_irrelevant'" in m and "Hong Kong Mahjong" in m for m in rejected
    )


def test_rejected_event_extraction_failed_reason_when_no_date(caplog):
    # Evidence with no parseable event date -> extraction fails -> rejected.
    source = _source(
        meetup_results=[
            {
                "title": "AWS Builders Austin",
                "url": "https://www.meetup.com/a/7/",
                "text": "An AWS community in Austin, Texas. (no explicit event date here)",
            }
        ],
        luma_results=[],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    rejected = [m for m in lines if "status='rejected'" in m]
    assert any("reason='extraction_failed'" in m for m in rejected)


# --------------------------------------------------------------------------- #
# Per-source summary logging
# --------------------------------------------------------------------------- #
def test_per_source_summary_logging(caplog):
    source = _source(
        meetup_results=[
            _result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/"),
            _result("Hong Kong Mahjong", "https://www.meetup.com/a/9/"),
        ],
        luma_results=[_result("AWS Bedrock Workshop", "https://lu.ma/austin-bedrock")],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    lines = _diag_lines(caplog)
    # meetup summary: 2 raw, 2 extracted, 1 accepted, 1 rejected.
    assert any(
        "source='meetup'" in m and "raw_results=2" in m and "accepted_events=1" in m
        and "rejected_events=1" in m and "extracted_candidates=2" in m
        for m in lines
    )
    # luma summary: 1 raw, 1 accepted.
    assert any(
        "source='luma'" in m and "raw_results=1" in m and "accepted_events=1" in m
        for m in lines
    )


# --------------------------------------------------------------------------- #
# Security: credential / header / token values are never logged
# --------------------------------------------------------------------------- #
def test_no_sensitive_values_are_ever_logged(caplog):
    # Inject sensitive-looking values into fields the source does NOT log (the diag
    # only logs title/url/evidence/date/city). These must never appear in any log line.
    secret_signals = [
        "AKIAIOSFODNN7EXAMPLE",              # access key id
        "wJalrXUtnFEMI/K7MDENG/bEXAMPLEKEY",  # secret access key
        "Authorization",
        "Bearer sk-secret-token",
        "X-Amz-Security-Token",
        "aws_session_token=FwoGZ",
        "SignedHeaders=host",
        "Signature=abcdef",
    ]
    poisoned = {
        "title": "AWS Cloud Deep Dive",
        "url": "https://www.meetup.com/a/1/",
        "text": "Austin, Texas on 2026-10-07T18:30:00Z about AWS Cloud Deep Dive.",
        # Sensitive values live only in non-logged fields.
        "authorization": "Bearer sk-secret-token",
        "headers": {"Authorization": "AKIAIOSFODNN7EXAMPLE", "Cookie": "session=abc"},
        "credentials": "wJalrXUtnFEMI/K7MDENG/bEXAMPLEKEY",
        "session_token": "FwoGZ",
    }
    source = _source(meetup_results=[poisoned], luma_results=[])

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        source(["AWS"], *WINDOW)

    all_logs = "\n".join(r.getMessage() for r in caplog.records)
    for signal in secret_signals:
        assert signal not in all_logs, f"sensitive value leaked into logs: {signal!r}"


# --------------------------------------------------------------------------- #
# Behavior is unchanged by the diagnostics
# --------------------------------------------------------------------------- #
def test_diagnostics_do_not_change_returned_events(caplog):
    source = _source(
        meetup_results=[
            _result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/"),
            _result("Hong Kong Mahjong", "https://www.meetup.com/a/9/"),  # off-topic
        ],
        luma_results=[_result("AWS Bedrock Workshop", "https://lu.ma/austin-bedrock")],
    )
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        out = source(["AWS"], *WINDOW)

    # Only the two on-topic events are returned; URLs preserved byte-for-byte.
    titles = sorted(e.title for e in out)
    assert titles == ["AWS Bedrock Workshop", "AWS Cloud Deep Dive"]
    urls = {e.event_url for e in out}
    assert urls == {"https://www.meetup.com/a/1/", "https://lu.ma/austin-bedrock"}
    assert {e.source_platform for e in out} == {"Meetup", "Luma"}
