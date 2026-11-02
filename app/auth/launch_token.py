from __future__ import annotations

import logging
from typing import Any

from app.auth.rbac import AccessScope, build_scope
from app.core.config import get_settings
from app.core.security import decrypt_hrms_token, sign_session
from app.db.hrms import hrms_execute

log = logging.getLogger(__name__)


def load_user_company(user_id: int) -> dict[str, Any] | None:
    rows = hrms_execute(
        """
        SELECT TOP 1 UserID, EmployeeID, CompanyID, AccessID, LocationIds, OrgStructureIds
        FROM dbo.adm_user_company
        WHERE UserID = :uid
        ORDER BY ID DESC
        """,
        {"uid": user_id},
    )
    return rows[0] if rows else None


def authenticate_launch_userid(userid: int) -> tuple[AccessScope, str] | None:
    """Validate HRMS launch userid via fresh token; return scope + session cookie value.

    Fail-closed when REQUIRE_TOKEN_DECRYPT=1 (default): decrypt must succeed when a
    passphrase is configured. Opaque-token environments can set REQUIRE_TOKEN_DECRYPT=0.
    """
    rows = hrms_execute(
        """
        SELECT TOP 1 ID, UserID, Token, IsExpired, ExpiryDate
        FROM dbo.adm_user_token
        WHERE UserID = :uid AND IsExpired = 0 AND ExpiryDate > GETDATE()
        ORDER BY ID DESC
        """,
        {"uid": userid},
    )
    if not rows:
        log.info("No valid token for user %s", userid)
        return None
    token = rows[0].get("Token") or ""
    settings = get_settings()
    passphrase = settings.hrms_token_passphrase
    if settings.require_token_decrypt:
        if not passphrase:
            log.warning("REQUIRE_TOKEN_DECRYPT set but HRMS_TOKEN_PASSPHRASE empty — deny login")
            return None
        if not token:
            log.info("Empty token for user %s — deny (fail-closed)", userid)
            return None
        try:
            _ = decrypt_hrms_token(str(token), passphrase)
        except Exception as e:
            log.info("Token decrypt failed for user %s — deny (fail-closed): %s", userid, e)
            return None
    else:
        try:
            if token and passphrase:
                _ = decrypt_hrms_token(str(token), passphrase)
        except Exception as e:
            log.debug("Token decrypt skipped/failed for user %s: %s", userid, e)

    company = load_user_company(userid)
    scope = build_scope(company)
    if company:
        scope.user_id = userid
    session = sign_session(
        {
            "user_id": userid,
            "employee_id": scope.employee_id,
            "company_id": scope.company_id,
            "access_id": scope.access_id,
            "location_ids": scope.location_ids,
            "org_structure_ids": scope.org_structure_ids,
        }
    )
    return scope, session


def scope_from_session_payload(payload: dict[str, Any]) -> AccessScope:
    aid = payload.get("access_id")
    if aid is None:
        return AccessScope(access_id=None, denied=True, deny_reason="no access_id in session")
    return AccessScope(
        access_id=int(aid),
        user_id=payload.get("user_id"),
        employee_id=payload.get("employee_id"),
        company_id=payload.get("company_id"),
        location_ids=list(payload.get("location_ids") or []),
        org_structure_ids=list(payload.get("org_structure_ids") or []),
    )
