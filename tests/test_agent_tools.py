"""Full explorer tool surface tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.auth.rbac import AccessScope
from app.orchestrator.tool_specs import TOOL_SPECS
from app.orchestrator.tools import (
    describe_table,
    dispatch_tool,
    list_sanction_allocations,
    search_designations,
    search_grades,
    search_locations,
    search_org_levels,
    search_positions,
    tool_sanction_strength,
)


def test_tool_catalog_covers_all_sources():
    names = {t["name"] for t in TOOL_SPECS}
    for required in (
        "search_positions",
        "search_grades",
        "describe_table",
        "list_sanction_allocations",
        "pf_pages",
        "pf_events",
        "pf_segments",
        "pf_files",
        "pf_vector_search",
        "list_policy_docs",
        "list_policy_doc_types",
        "list_schedule_ii",
        "jd_authority",
        "policy_corpus_stats",
    ):
        assert required in names


@pytest.mark.integration
def test_search_designations_additional_director():
    out = search_designations("additional director", limit=20)
    assert out["ok"] is True
    names = [str(r["DesignationName"]).strip().lower() for r in out["rows"]]
    assert any(n == "additional director" for n in names)


@pytest.mark.integration
def test_search_locations_hyderabad():
    out = search_locations("hyderabad", limit=20)
    assert out["ok"] is True
    assert any("hyderabad" in str(r["LocationName"]).lower() for r in out["rows"])


@pytest.mark.integration
def test_search_org_irs():
    out = search_org_levels("IRS")
    assert out["ok"] is True
    assert any(int(r["ID"]) == 11 or str(r.get("Name")).upper() == "IRS" for r in out["rows"])


@pytest.mark.integration
def test_search_grades_bs18():
    out = search_grades("BS-18")
    assert out["ok"] is True
    assert out["rows"]


@pytest.mark.integration
def test_search_positions_with_filters():
    out = search_positions(
        {
            "designation_query": "additional director",
            "location_query": "hyderabad",
            "org_query": "IRS",
            "limit": 20,
        }
    )
    assert out["ok"] is True
    # May be zero chairs in MF even when allocation exists — still a valid explore result
    assert "rows" in out
    assert "filters" in out


@pytest.mark.integration
def test_describe_table_employee():
    out = describe_table("pr_employee_mf")
    assert out["ok"] is True
    assert "DesignationID" in (out.get("columns") or [])


@pytest.mark.integration
def test_list_allocations_and_sanction():
    alloc = list_sanction_allocations(
        {
            "designation_query": "additional director",
            "location_query": "hyderabad",
            "org_query": "IRS",
            "limit": 20,
        }
    )
    assert alloc["ok"] is True
    assert alloc["total_sanctioned"] == 3

    scope = AccessScope(access_id=0, user_id=1)
    out = tool_sanction_strength(
        {
            "lens": "designation",
            "designation_query": "additional director",
            "location_query": "hyderabad",
            "org_query": "IRS",
        },
        scope,
    )
    assert out.get("ok") is True
    assert out.get("sanctioned") == 3


@pytest.mark.integration
def test_position_lens_sanction():
    scope = AccessScope(access_id=0, user_id=1)
    out = tool_sanction_strength(
        {
            "lens": "position",
            "designation_query": "additional director",
            "location_query": "hyderabad",
            "org_query": "IRS",
        },
        scope,
    )
    assert out.get("ok") is True
    assert out.get("lens") == "position"
    assert "sanctioned" in out


def test_policy_list_tools_no_db_crash():
    scope = AccessScope(access_id=0, user_id=1)
    docs = dispatch_tool("list_policy_docs", {"limit": 10}, scope=scope, question="x")
    assert docs.get("ok") is True
    types = dispatch_tool("list_policy_doc_types", {}, scope=scope, question="x")
    assert types.get("ok") is True
    stats = dispatch_tool("policy_corpus_stats", {}, scope=scope, question="x")
    assert stats.get("ok") is True


def test_finish_tool_dispatch():
    scope = AccessScope(access_id=0, user_id=1)
    out = dispatch_tool(
        "finish",
        {"answer_hint": "sanctioned=3", "topic": "sanction"},
        scope=scope,
        question="test",
    )
    assert out["finished"] is True


def test_agent_pipeline_uses_run_agent():
    from app.core.config import get_settings
    from app.orchestrator import graph

    get_settings.cache_clear()
    scope = AccessScope(access_id=0, user_id=1)
    with patch(
        "app.orchestrator.graph.run_agent",
        return_value={
            "answer": "ok",
            "evidence": [],
            "rule_trace": [],
            "topic": "sanction",
            "route": {"mode": "agentic"},
            "hrms": {},
            "pf": {},
            "policy": [],
            "rules": {},
            "sources_used": {"hrms": True},
            "abstain_reasons": [],
        },
    ) as mocked:
        assert get_settings().agentic_explorer is True
        result = graph.run_pipeline("any question", scope)
        mocked.assert_called_once()
        assert result["route"]["mode"] == "agentic"
    get_settings.cache_clear()
