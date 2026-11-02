"""LLM-primary routing tests (chat_json mocked — no live LLM)."""
from __future__ import annotations

from unittest.mock import patch

from app.orchestrator.graph import route_intent, _fallback_route


def test_llm_reports_to_uses_policy_not_forced_hrms():
    with patch(
        "app.orchestrator.graph.chat_json",
        return_value={
            "hrms": False,
            "pf": False,
            "policy": True,
            "rules": ["jd"],
            "topic": "jd",
            "employee_name": None,
            "cnic": None,
            "lens": None,
            "grade_id": None,
            "designation_id": None,
            "position_id": None,
            "target_bps": None,
            "from_designation": None,
            "to_designation": None,
            "course_name": None,
            "office_type": "HQ",
        },
    ):
        r = route_intent("Who does the Chief Admin/HR report to?")
    assert r["policy"] is True
    assert r["hrms"] is False  # must NOT be forced by 'who'
    assert r["routing"] == "llm"
    assert "jd" in r["rules"]


def test_llm_who_is_named_officer_can_use_hrms():
    with patch(
        "app.orchestrator.graph.chat_json",
        return_value={
            "hrms": True,
            "pf": False,
            "policy": False,
            "rules": [],
            "topic": "identity",
            "employee_name": "Ali Khan",
            "cnic": None,
        },
    ):
        r = route_intent("Who is Ali Khan?")
    assert r["hrms"] is True
    assert r["policy"] is False


def test_cnic_safety_net_enables_pf_and_hrms():
    with patch(
        "app.orchestrator.graph.chat_json",
        return_value={
            "hrms": False,
            "pf": False,
            "policy": True,
            "rules": [],
            "topic": "policy",
            "cnic": None,
        },
    ):
        r = route_intent("Any note for CNIC 35202-1234567-1 in rules?")
    assert r["cnic"] == "35202-1234567-1"
    assert r["pf"] is True
    assert r["hrms"] is True
    assert r["policy"] is True


def test_fallback_reports_to_prefers_policy():
    r = _fallback_route("Who does Chief Admin report to according to JD?")
    assert r["policy"] is True
    assert r["routing"] == "fallback"
    # Must not be HRMS-only
    assert r["policy"] or r["pf"]


def test_llm_failure_uses_fallback_not_keyword_who_lock():
    with patch("app.orchestrator.graph.chat_json", side_effect=RuntimeError("llm down")):
        r = route_intent("Who does the Director General report to?")
    assert r["routing"] == "fallback"
    assert r["policy"] is True
