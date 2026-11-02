"""Production auth / RBAC hardening unit tests (no live DB required)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.rbac import AccessScope, sql_scope_predicate, filter_rows_by_scope
from app.core.config import get_settings
from app.core.rate_limit import RateLimiter
from app.core.security import sign_session


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_session_secret_is_strong():
    s = get_settings()
    assert len(s.session_secret) >= 24
    assert s.session_secret.lower() not in {"change-me", "secret", "dev"}


def test_open_access_disabled_in_production():
    s = get_settings()
    # .env sets ENVIRONMENT=production
    assert s.environment == "production"
    assert s.open_access_allowed is False


def test_sql_scope_predicates():
    full = AccessScope(access_id=0)
    pred, params = sql_scope_predicate(full)
    assert pred == "1=1"

    self_s = AccessScope(access_id=3, employee_id=42)
    pred, params = sql_scope_predicate(self_s)
    assert "e.ID" in pred
    assert params["_scope_eid"] == 42

    loc = AccessScope(access_id=1, location_ids=[10, 20])
    pred, params = sql_scope_predicate(loc)
    assert "LocationID" in pred
    assert len(params) == 2

    denied = AccessScope(access_id=None, denied=True)
    pred, _ = sql_scope_predicate(denied)
    assert pred == "1=0"


def test_filter_rows_by_scope():
    rows = [
        {"ID": 1, "LocationID": 10},
        {"ID": 2, "LocationID": 99},
    ]
    scope = AccessScope(access_id=1, location_ids=[10])
    filtered = filter_rows_by_scope(scope, rows)
    assert len(filtered) == 1
    assert filtered[0]["ID"] == 1


def test_rate_limiter():
    lim = RateLimiter(max_per_minute=3)
    assert lim.allow("a")
    assert lim.allow("a")
    assert lim.allow("a")
    assert not lim.allow("a")
    assert lim.allow("b")


def test_chat_ask_requires_auth():
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/v1/chat/ask", json={"question": "sanctioned strength BS-17"})
    assert r.status_code == 401


def test_chat_ask_with_signed_session():
    from app.main import app

    token = sign_session(
        {
            "user_id": 1,
            "employee_id": 100,
            "company_id": 1,
            "access_id": 0,
            "location_ids": [],
            "org_structure_ids": [],
        }
    )
    client = TestClient(app)
    # Health should still work without auth
    h = client.get("/api/v1/health")
    assert h.status_code == 200
    # me with cookie
    r = client.get("/api/v1/me", cookies={"fbr_session": token})
    assert r.status_code == 200
    body = r.json()
    assert body.get("access_id") == 0
    assert body.get("authenticated") is True


def test_no_hardcoded_demo_bps_in_graph_source():
    """Guardrail: orchestrator must not ship demo BPS-18 fixtures."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "graph.py"
    text = src.read_text(encoding="utf-8")
    assert 'seat_bps=18' not in text
    assert '"bps": 18' not in text
    assert "DOB\": \"1985-01-01\"" not in text
    assert "hq_feb_2022" in text  # authority id ok; must come from live JD resolve path
