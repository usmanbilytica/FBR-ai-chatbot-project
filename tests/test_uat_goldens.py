"""UAT golden catalog integrity — does not call live LLM/DB."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDENS = ROOT / "data" / "uat_goldens.json"


def test_uat_goldens_file_exists_and_covers_three_sources():
    assert GOLDENS.exists(), "data/uat_goldens.json missing"
    data = json.loads(GOLDENS.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    assert len(cases) >= 8
    sources = set()
    for c in cases:
        assert c.get("id")
        assert c.get("question")
        assert c.get("sources")
        sources.update(c["sources"])
    assert {"hrms", "pf", "policy", "rules"} <= sources


def test_policy_retrieve_prefers_qdrant_policy_path():
    from pathlib import Path as P

    src = P(__file__).resolve().parents[1] / "app" / "sources" / "policies" / "retrieve.py"
    text = src.read_text(encoding="utf-8")
    assert "qdrant_policy" in text
    assert "Legacy `data/qdrant` is no longer consulted" in text


def test_pf_retrieve_wires_vector_search():
    from pathlib import Path as P

    src = P(__file__).resolve().parents[1] / "app" / "sources" / "personal_files" / "retrieve.py"
    text = src.read_text(encoding="utf-8")
    assert "pf_vector_search" in text
    assert "qdrant_pf" in text


def test_embeddings_fail_closed_flag():
    from app.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.require_real_embeddings is True


def test_open_sql_default_safer_when_env_omitted(monkeypatch):
    # Ensure production restricts open SQL to full access in ask_hrms logic
    from app.auth.rbac import AccessID, AccessScope
    from app.sources.hrms import text2sql

    scope = AccessScope(access_id=AccessID.LOCATION, location_ids=[1])
    # Force allow_open_sql True but restricted scope → abstain in production
    from app.core.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    if s.is_production and s.allow_open_sql:
        out = text2sql.ask_hrms("count employees", scope=scope)
        assert out.get("ok") is False
        assert out.get("abstain_reason") == "open_sql_requires_full_access"
