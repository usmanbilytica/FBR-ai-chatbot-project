from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable


class AccessID(IntEnum):
    FULL = 0
    LOCATION = 1
    ORG = 2
    SELF = 3


SENSITIVE_SELF = frozenset(
    {"discipline", "medical", "family", "complaint", "inquiry", "suspension", "ed"}
)
SENSITIVE_LOC_ORG = frozenset({"medical"})


@dataclass
class AccessScope:
    access_id: int | None
    user_id: int | None = None
    employee_id: int | None = None
    company_id: int | None = None
    location_ids: list[int] = field(default_factory=list)
    org_structure_ids: list[int] = field(default_factory=list)
    denied: bool = False
    deny_reason: str = ""

    @property
    def level(self) -> AccessID | None:
        if self.access_id is None:
            return None
        try:
            return AccessID(int(self.access_id))
        except ValueError:
            return None


def parse_csv_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def build_scope(row: dict[str, Any] | None) -> AccessScope:
    if not row:
        return AccessScope(access_id=None, denied=True, deny_reason="missing access row")
    aid = row.get("AccessID")
    if aid is None or str(aid).strip() == "":
        return AccessScope(access_id=None, denied=True, deny_reason="empty AccessID")
    return AccessScope(
        access_id=int(aid),
        user_id=int(row["UserID"]) if row.get("UserID") is not None else None,
        employee_id=int(row["EmployeeID"]) if row.get("EmployeeID") is not None else None,
        company_id=int(row["CompanyID"]) if row.get("CompanyID") is not None else None,
        location_ids=parse_csv_ids(row.get("LocationIds")),
        org_structure_ids=parse_csv_ids(row.get("OrgStructureIds")),
    )


def can_recommend(scope: AccessScope) -> tuple[bool, str]:
    if scope.denied or scope.level is None:
        return False, scope.deny_reason or "access denied"
    if scope.level == AccessID.SELF:
        return False, "self scope cannot run org DSS recommend/succession"
    return True, ""


def can_finalize(scope: AccessScope) -> tuple[bool, str]:
    if scope.denied or scope.level is None:
        return False, scope.deny_reason or "access denied"
    if scope.level != AccessID.FULL:
        return False, "finalize requires full access (AccessID 0)"
    return True, ""


def can_org_shortlist(scope: AccessScope) -> tuple[bool, str]:
    if scope.denied or scope.level is None:
        return False, "denied"
    if scope.level == AccessID.SELF:
        return False, "self scope blocked from org-wide shortlists"
    return True, ""


def redact_topics(scope: AccessScope, topics: Iterable[str]) -> list[str]:
    topics = list(topics)
    if scope.level == AccessID.SELF:
        return [t for t in topics if t.lower() not in SENSITIVE_SELF]
    if scope.level in (AccessID.LOCATION, AccessID.ORG):
        return [t for t in topics if t.lower() not in SENSITIVE_LOC_ORG]
    return topics


def employee_in_scope(scope: AccessScope, employee: dict[str, Any]) -> bool:
    if scope.denied or scope.level is None:
        return False
    if scope.level == AccessID.FULL:
        return True
    if scope.level == AccessID.SELF:
        return scope.employee_id is not None and int(employee.get("ID") or employee.get("EmployeeID") or -1) == scope.employee_id
    if scope.level == AccessID.LOCATION:
        loc = employee.get("LocationID") or employee.get("WorkingLocationID")
        return loc is not None and int(loc) in scope.location_ids
    if scope.level == AccessID.ORG:
        org_ids = {
            int(employee[k])
            for k in (f"OrgLevel{i}" for i in range(2, 10))
            if employee.get(k) is not None
        }
        return bool(org_ids & set(scope.org_structure_ids))
    return False


def sql_scope_predicate(scope: AccessScope, alias: str = "e") -> tuple[str, dict[str, Any]]:
    """Return a SQL AND-clause + params restricting pr_employee_mf-style rows to AccessScope.

    Callers must AND this into employee queries. Full access returns (1=1, {}).
    Denied / unknown returns (1=0, {}).
    """
    if scope.denied or scope.level is None:
        return "1=0", {}
    if scope.level == AccessID.FULL:
        return "1=1", {}
    if scope.level == AccessID.SELF:
        if scope.employee_id is None:
            return "1=0", {}
        return f"{alias}.ID = :_scope_eid", {"_scope_eid": int(scope.employee_id)}
    if scope.level == AccessID.LOCATION:
        if not scope.location_ids:
            return "1=0", {}
        params: dict[str, Any] = {}
        parts = []
        for i, lid in enumerate(scope.location_ids):
            params[f"_scope_lid{i}"] = int(lid)
            parts.append(f":_scope_lid{i}")
        csv = ", ".join(parts)
        return (
            f"({alias}.LocationID IN ({csv}) OR {alias}.WorkingLocationID IN ({csv}))",
            params,
        )
    if scope.level == AccessID.ORG:
        if not scope.org_structure_ids:
            return "1=0", {}
        params = {}
        parts = []
        for i, oid in enumerate(scope.org_structure_ids):
            params[f"_scope_oid{i}"] = int(oid)
            parts.append(f":_scope_oid{i}")
        csv = ", ".join(parts)
        org_cols = " OR ".join(f"{alias}.OrgLevel{i} IN ({csv})" for i in range(2, 10))
        return f"({org_cols})", params
    return "1=0", {}


def filter_rows_by_scope(scope: AccessScope, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if employee_in_scope(scope, r)]
