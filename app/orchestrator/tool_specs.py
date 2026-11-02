"""Tool catalog for the multi-source agent (HRMS + PF + Policy + rules)."""
from __future__ import annotations

from typing import Any

TOOL_SPECS: list[dict[str, Any]] = [
    # —— HRMS entity + schema ——
    {
        "name": "search_catalog",
        "desc": "BM25 search HRMS schema catalog for relevant tables.",
        "args": {"query": "str", "top_k": "int?"},
    },
    {
        "name": "describe_table",
        "desc": "Return columns/PK/FKs for one HRMS table from the catalog.",
        "args": {"table": "str"},
    },
    {
        "name": "search_designations",
        "desc": "Fuzzy-search pr_designation by title.",
        "args": {"query": "str", "limit": "int?"},
    },
    {
        "name": "search_locations",
        "desc": "Fuzzy-search adm_company_location.",
        "args": {"query": "str", "limit": "int?"},
    },
    {
        "name": "search_org_levels",
        "desc": "Search adm_org_structure (IRS=11, PCS=12, wings/formations).",
        "args": {"query": "str", "limit": "int?"},
    },
    {
        "name": "search_grades",
        "desc": "Search pr_employee_grades_mf by BPS/code/name.",
        "args": {"query": "str", "limit": "int?"},
    },
    {
        "name": "search_positions",
        "desc": "Fuzzy-search pr_position_mf (title and/or designation+location+org filters).",
        "args": {
            "query": "str?",
            "designation_query": "str?",
            "location_query": "str?",
            "org_query": "str?",
            "grade_query": "str?",
            "vacant_only": "bool?",
            "limit": "int?",
        },
    },
    {
        "name": "resolve_officer",
        "desc": "Resolve live HRMS officer by CNIC / name / employee_id.",
        "args": {"cnic": "str?", "name": "str?", "employee_id": "int?"},
    },
    {
        "name": "list_sanction_allocations",
        "desc": "List rows from pr_designation_sanctioned_allocation with optional filters.",
        "args": {
            "designation_query": "str?",
            "location_query": "str?",
            "org_query": "str?",
            "designation_id": "int?",
            "location_id": "int?",
            "org_level2": "int?",
            "limit": "int?",
        },
    },
    {
        "name": "run_hrms_sql",
        "desc": "Catalog-validated SELECT/WITH against live HRMS (AccessScope applied).",
        "args": {"sql": "str"},
    },
    {
        "name": "sanction_strength",
        "desc": "Sanctioned/occupied/vacant for grade|designation|position lenses; accepts IDs or name queries.",
        "args": {
            "lens": "grade|designation|position",
            "grade_id": "int?",
            "designation_id": "int?",
            "location_id": "int?",
            "org_level2": "int?",
            "position_id": "int?",
            "grade_query": "str?",
            "designation_query": "str?",
            "location_query": "str?",
            "org_query": "str?",
            "position_query": "str?",
        },
    },
    # —— Personal Files (OCR) facets ——
    {
        "name": "pf_resolve",
        "desc": "Resolve officer CNIC bridge for Personal Files.",
        "args": {"cnic": "str?", "name": "str?", "employee_id": "int?"},
    },
    {
        "name": "pf_categories",
        "desc": "List PF document categories for an officer (CNIC/name).",
        "args": {"cnic": "str?", "name": "str?"},
    },
    {
        "name": "pf_pages",
        "desc": "PF page hits; optional topics and text query.",
        "args": {"cnic": "str?", "name": "str?", "topics": "list[str]?", "query_text": "str?", "limit": "int?"},
    },
    {
        "name": "pf_events",
        "desc": "PF timeline events; optional topics.",
        "args": {"cnic": "str?", "name": "str?", "topics": "list[str]?", "limit": "int?"},
    },
    {
        "name": "pf_segments",
        "desc": "PF document segments; optional topics.",
        "args": {"cnic": "str?", "name": "str?", "topics": "list[str]?", "limit": "int?"},
    },
    {
        "name": "pf_files",
        "desc": "PF source PDF/file list for an officer.",
        "args": {"cnic": "str?", "name": "str?", "limit": "int?"},
    },
    {
        "name": "pf_profile",
        "desc": "PF 360 profile row for an officer.",
        "args": {"cnic": "str?", "name": "str?"},
    },
    {
        "name": "pf_vector_search",
        "desc": "Semantic PF search (qdrant_pf; best-effort if indexer holds lock).",
        "args": {"question": "str", "cnic": "str?", "name": "str?", "top_k": "int?"},
    },
    {
        "name": "pf_search",
        "desc": "Hybrid PF search (SQL pages/events + vector) for a natural question.",
        "args": {"question": "str", "cnic": "str?", "employee_name": "str?"},
    },
    # —— Policy corpus ——
    {
        "name": "search_policies",
        "desc": "Semantic/keyword policy+JD search; optional doc_type/office/jd filters.",
        "args": {
            "question": "str",
            "top_k": "int?",
            "doc_type": "str?",
            "office_type": "str?",
            "jd_only": "bool?",
            "doc_name_contains": "str?",
        },
    },
    {
        "name": "list_policy_docs",
        "desc": "List ingested policy/JD documents with chunk counts.",
        "args": {"limit": "int?"},
    },
    {
        "name": "list_policy_doc_types",
        "desc": "List policy doc_type histogram from policy_facts.",
        "args": {},
    },
    {
        "name": "policy_corpus_stats",
        "desc": "Chunk / Schedule-II / qdrant_policy point counts.",
        "args": {},
    },
    {
        "name": "jd_authority",
        "desc": "Load JD authority book registry (HQ/Field).",
        "args": {},
    },
    {
        "name": "schedule_ii",
        "desc": "Lookup one Schedule-II hop (from_bps → to_bps).",
        "args": {"from_bps": "int", "to_bps": "int"},
    },
    {
        "name": "list_schedule_ii",
        "desc": "List Schedule-II rows from policy_facts.",
        "args": {"limit": "int?"},
    },
    # —— Rules-as-tools ——
    {
        "name": "rule_promotion",
        "desc": "Cadre/Ex-Cadre promotion eligibility.",
        "args": {
            "cnic": "str?",
            "employee_name": "str?",
            "target_bps": "int?",
            "from_designation": "str?",
            "to_designation": "str?",
        },
    },
    {
        "name": "rule_succession",
        "desc": "Succession/transfer fill-mode for a seat.",
        "args": {
            "question": "str",
            "grade_id": "int?",
            "designation_id": "int?",
            "position_id": "int?",
            "cnic": "str?",
            "employee_name": "str?",
        },
    },
    {
        "name": "rule_training",
        "desc": "Training nomination eligibility.",
        "args": {"question": "str", "cnic": "str?", "employee_name": "str?", "course_name": "str?"},
    },
    {
        "name": "rule_jd",
        "desc": "JD hard-gate vs authority books + retrieved JD text.",
        "args": {"question": "str", "cnic": "str?", "employee_name": "str?", "office_type": "HQ|Field?"},
    },
    {
        "name": "finish",
        "desc": "Stop exploring; supply answer_hint from evidence.",
        "args": {"answer_hint": "str", "topic": "str?"},
    },
]


def tool_catalog_prompt() -> str:
    lines = ["Available tools (call ONE per turn as JSON):"]
    for t in TOOL_SPECS:
        lines.append(f"- {t['name']}: {t['desc']} args={t['args']}")
    lines.append(
        'Return ONLY JSON: {"tool":"<name>","args":{...}} '
        'or {"tool":"finish","args":{"answer_hint":"...","topic":"..."}}'
    )
    return "\n".join(lines)
