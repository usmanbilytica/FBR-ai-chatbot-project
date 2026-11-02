from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Cookie, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.auth.audit import flag_request, new_request_id, write_audit
from app.auth.launch_token import authenticate_launch_userid, scope_from_session_payload
from app.auth.rbac import AccessScope
from app.core.config import get_settings
from app.core.rate_limit import RateLimiter
from app.core.security import load_session
from app.db.hrms import hrms_ping
from app.db.ocr import ocr_ping
from app.orchestrator.graph import run_pipeline
from app.sources.hrms.live_resolve import cnic_coverage_report

router = APIRouter(prefix="/api/v1")

_limiter: RateLimiter | None = None


def _get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(get_settings().rate_limit_per_minute)
    return _limiter


class AskBody(BaseModel):
    question: str = Field(min_length=1)
    cnic: Optional[str] = None
    employee_name: Optional[str] = None


class FlagBody(BaseModel):
    request_id: str


class LoginBody(BaseModel):
    userid: int = Field(gt=0)


def _client_key(request: Request, scope: AccessScope | None = None) -> str:
    if scope and scope.user_id is not None:
        return f"u:{scope.user_id}"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return f"ip:{fwd.split(',')[0].strip()}"
    if request.client:
        return f"ip:{request.client.host}"
    return "ip:unknown"


def _check_rate(request: Request, scope: AccessScope | None = None) -> None:
    lim = _get_limiter()
    if not lim.allow(_client_key(request, scope)):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def _scope_from_cookie(session: str | None) -> AccessScope:
    """Production: no session ⇒ 401. Dev open-access only when explicitly enabled."""
    settings = get_settings()
    if not session:
        if settings.open_access_allowed:
            return AccessScope(access_id=0, user_id=0, employee_id=None)
        raise HTTPException(status_code=401, detail="authentication required")
    payload = load_session(session)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    scope = scope_from_session_payload(payload)
    if scope.denied or scope.access_id is None:
        raise HTTPException(status_code=403, detail=scope.deny_reason or "access denied")
    return scope


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        "fbr_session",
        token,
        httponly=True,
        samesite=s.cookie_samesite,  # type: ignore[arg-type]
        secure=s.cookie_secure or s.is_production,
        max_age=60 * 60 * 12,
        path="/",
    )


def _qdrant_counts() -> dict[str, Any]:
    """Best-effort point counts.

    Never opens `qdrant_pf` (Windows exclusive lock would interrupt live indexing).
    PF progress comes from `data/pf_index_checkpoint.json` written by the indexer.
    """
    import json
    from pathlib import Path

    out: dict[str, Any] = {
        "policy_points": None,
        "pf_points": None,
        "policy_store": None,
        "pf_store": None,
        "pf_note": None,
        "pf_checkpoint": None,
    }
    s = get_settings()
    out["policy_store"] = str(s.qdrant_policy_path)
    out["pf_store"] = str(s.qdrant_pf_path)

    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(path=str(s.qdrant_policy_path))
        try:
            if client.collection_exists(s.policy_collection):
                info = client.get_collection(s.policy_collection)
                out["policy_points"] = int(getattr(info, "points_count", 0) or 0)
        finally:
            client.close()
    except Exception as e:
        out["policy_error"] = str(e)[:200]

    cp_path = Path(s.data_dir) / "pf_index_checkpoint.json"
    if cp_path.exists():
        try:
            cp = json.loads(cp_path.read_text(encoding="utf-8"))
            out["pf_checkpoint"] = cp
            out["pf_points"] = int(cp.get("upserted") or 0)
            out["pf_note"] = (
                "PF count from indexer checkpoint (store not opened — avoids interrupting index)"
            )
        except Exception as e:
            out["pf_note"] = f"checkpoint unreadable: {e}"
    else:
        out["pf_note"] = "no pf_index_checkpoint.json yet"
    return out


@router.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    q = _qdrant_counts()
    return {
        "ok": True,
        "service": "FBR Greenfield Agentic HR DSS",
        "environment": s.environment,
        "hrms": hrms_ping(),
        "ocr": ocr_ping(),
        "status_map": s.status_map_path.exists(),
        "catalog": (s.catalog_dir / "schema_catalog.json").exists(),
        "policy_facts": s.policy_facts_db.exists(),
        "qdrant_policy": q.get("policy_store"),
        "qdrant_pf": q.get("pf_store"),
        "qdrant_counts": {
            "policy_points": q.get("policy_points"),
            "pf_points": q.get("pf_points"),
            "pf_note": q.get("pf_note"),
        },
        "cookie_secure": s.cookie_secure,
        "allow_open_sql": s.allow_open_sql,
        "require_token_decrypt": s.require_token_decrypt,
        "require_real_embeddings": s.require_real_embeddings,
    }


@router.get("/ready")
def ready() -> dict[str, Any]:
    """Readiness probe — fails if critical deps are missing."""
    s = get_settings()
    q = _qdrant_counts()
    checks = {
        "hrms": hrms_ping(),
        "ocr": ocr_ping(),
        "status_map": s.status_map_path.exists(),
        "catalog": (s.catalog_dir / "schema_catalog.json").exists(),
        "session_secret": len(s.session_secret) >= 24,
        "policy_facts": s.policy_facts_db.exists(),
        "policy_vectors_present": bool(q.get("policy_points") and q["policy_points"] > 0),
        # PF vectors optional while indexer runs — never fail ready on lock
        "pf_store_configured": s.qdrant_pf_path.exists(),
    }
    details = {
        "policy_points": q.get("policy_points"),
        "pf_points": q.get("pf_points"),
        "pf_note": q.get("pf_note"),
    }
    ok = all(checks.values())
    if not ok:
        raise HTTPException(status_code=503, detail={"ok": False, "checks": checks, "details": details})
    return {"ok": True, "checks": checks, "details": details}


@router.get("/ops/cnic-coverage")
def ops_cnic_coverage(
    request: Request,
    session: Optional[str] = Cookie(default=None, alias="fbr_session"),
) -> dict[str, Any]:
    scope = _scope_from_cookie(session)
    _check_rate(request, scope)
    if scope.access_id != 0:
        raise HTTPException(status_code=403, detail="full access required")
    return cnic_coverage_report()


@router.post("/auth/login")
def auth_login(body: LoginBody, response: Response) -> dict[str, Any]:
    """Establish session from HRMS launch userid (validates adm_user_token)."""
    auth = authenticate_launch_userid(body.userid)
    if not auth:
        raise HTTPException(status_code=401, detail="launch auth failed — no valid HRMS token")
    scope, token = auth
    if scope.denied:
        raise HTTPException(status_code=403, detail=scope.deny_reason or "denied")
    _set_session_cookie(response, token)
    return {
        "ok": True,
        "user_id": scope.user_id,
        "employee_id": scope.employee_id,
        "company_id": scope.company_id,
        "access_id": scope.access_id,
        "location_ids": scope.location_ids,
        "org_structure_ids": scope.org_structure_ids,
    }


@router.post("/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie("fbr_session", path="/")
    return {"ok": True}


@router.get("/me")
def me(
    request: Request,
    userid: Optional[int] = Query(None),
    session: Optional[str] = Cookie(default=None, alias="fbr_session"),
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if userid is not None:
        auth = authenticate_launch_userid(userid)
        if not auth:
            raise HTTPException(status_code=401, detail="launch auth failed")
        scope, token = auth
        _set_session_cookie(response, token)
        return {
            "user_id": scope.user_id,
            "employee_id": scope.employee_id,
            "company_id": scope.company_id,
            "access_id": scope.access_id,
            "location_ids": scope.location_ids,
            "org_structure_ids": scope.org_structure_ids,
            "authenticated": True,
        }
    # Cookie-required (or explicit dev open access)
    try:
        scope = _scope_from_cookie(session)
    except HTTPException:
        settings = get_settings()
        if settings.open_access_allowed:
            return {
                "authenticated": False,
                "access_id": None,
                "note": "dev open access available — call /auth/login or pass ?userid=",
            }
        raise
    return {
        "authenticated": True,
        "user_id": scope.user_id,
        "employee_id": scope.employee_id,
        "access_id": scope.access_id,
        "location_ids": scope.location_ids,
        "org_structure_ids": scope.org_structure_ids,
        "denied": scope.denied,
    }


@router.get("/chat/suggestions")
def suggestions() -> dict[str, list[str]]:
    return {
        "suggestions": [
            "What is the sanctioned strength by grade for BS-18?",
            "Show position-level vacancy for grade BS-17",
            "Who is eligible for permanent transfer to a vacant BS-18 seat?",
            "Check OPS fill-mode rules for a BS-19 seat",
            "Can an IRS officer take a PCS cadre seat?",
            "Promotion eligibility from BS-18 to BS-19 including MCMC",
            "Find Personal File events for disciplinary / E&D matters",
            "What does the Civil Servants APT Rules say about promotion?",
            "Training nomination eligibility excluding LMS-named courses",
            "Designation sanctioned strength for a given designation and location",
        ]
    }


@router.post("/chat/ask")
def chat_ask(
    body: AskBody,
    request: Request,
    session: Optional[str] = Cookie(default=None, alias="fbr_session"),
) -> dict[str, Any]:
    scope = _scope_from_cookie(session)
    _check_rate(request, scope)
    if scope.denied:
        raise HTTPException(status_code=403, detail=scope.deny_reason or "denied")
    q = body.question
    if body.cnic:
        q = f"{q}\nCNIC: {body.cnic}"
    if body.employee_name:
        q = f"{q}\nOfficer name: {body.employee_name}"
    rid = new_request_id()
    t0 = time.time()
    result = run_pipeline(q, scope)
    ms = (time.time() - t0) * 1000
    write_audit(
        request_id=rid,
        user_id=scope.user_id,
        access_id=scope.access_id,
        question=body.question,
        topic=result.get("topic") or "general",
        answer=result.get("answer") or "",
        sql_events=[result.get("hrms")],
        pf_events=[result.get("pf")],
        policy_events=result.get("policy"),
        rule_events=result.get("rule_trace"),
        duration_ms=ms,
    )
    return {
        "request_id": rid,
        "answer": result.get("answer"),
        "evidence": result.get("evidence"),
        "rule_trace": result.get("rule_trace"),
        "topic": result.get("topic"),
        "sources_used": result.get("sources_used"),
        "abstain_reasons": result.get("abstain_reasons"),
        "duration_ms": ms,
    }


@router.post("/chat/ask/stream")
async def chat_ask_stream(
    body: AskBody,
    request: Request,
    session: Optional[str] = Cookie(default=None, alias="fbr_session"),
):
    scope = _scope_from_cookie(session)
    _check_rate(request, scope)
    if scope.denied:
        raise HTTPException(status_code=403, detail=scope.deny_reason or "denied")

    async def gen() -> AsyncIterator[dict[str, str]]:
        rid = new_request_id()
        steps: list[dict[str, Any]] = []

        def on_step(p: dict[str, Any]) -> None:
            steps.append(p)

        q = body.question
        t0 = time.time()
        result = run_pipeline(q, scope, on_step=on_step)
        for s in steps:
            yield {"event": "step", "data": json.dumps(s)}
        ms = (time.time() - t0) * 1000
        write_audit(
            request_id=rid,
            user_id=scope.user_id,
            access_id=scope.access_id,
            question=body.question,
            topic=result.get("topic") or "general",
            answer=result.get("answer") or "",
            sql_events=[result.get("hrms")],
            pf_events=[result.get("pf")],
            policy_events=result.get("policy"),
            rule_events=result.get("rule_trace"),
            duration_ms=ms,
        )
        done = {
            "request_id": rid,
            "answer": result.get("answer"),
            "evidence": result.get("evidence"),
            "rule_trace": result.get("rule_trace"),
            "topic": result.get("topic"),
            "sources_used": result.get("sources_used"),
            "abstain_reasons": result.get("abstain_reasons"),
            "duration_ms": ms,
        }
        yield {"event": "done", "data": json.dumps(done, default=str)}

    return EventSourceResponse(gen())


@router.post("/chat/flag")
def chat_flag(
    body: FlagBody,
    request: Request,
    session: Optional[str] = Cookie(default=None, alias="fbr_session"),
) -> dict[str, Any]:
    scope = _scope_from_cookie(session)
    _check_rate(request, scope)
    ok = flag_request(body.request_id)
    if not ok:
        raise HTTPException(status_code=404, detail="request not found")
    return {"ok": True}
