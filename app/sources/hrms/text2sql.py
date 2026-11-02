from __future__ import annotations

import logging
import re
from typing import Any

from app.auth.rbac import AccessID, AccessScope, filter_rows_by_scope, sql_scope_predicate
from app.core.config import get_settings
from app.db.hrms import hrms_execute
from app.services.llm import chat_completion
from app.sources.hrms.catalog import link_tables, load_catalog, schema_pack

log = logging.getLogger(__name__)

_DML = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|MERGE|EXEC|EXECUTE|CREATE)\b", re.I)


def extract_sql(text: str) -> str:
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.lower().startswith("sql"):
                p = p[3:].strip()
            if p.lower().startswith("select") or p.lower().startswith("with"):
                return p.strip().rstrip(";")
    m = re.search(r"(?is)(with\b[\s\S]+select\b[\s\S]+|select\b[\s\S]+)", t)
    if m:
        return m.group(1).strip().rstrip(";")
    return t.rstrip(";")


def validate_sql(sql: str) -> tuple[bool, str]:
    s = sql.strip()
    if not s:
        return False, "empty SQL"
    if _DML.search(s):
        return False, "only SELECT/WITH queries allowed"
    if not re.match(r"(?is)^(with|select)\b", s):
        return False, "must start with SELECT or WITH"
    catalog = load_catalog()
    tables = catalog.get("tables") or {}
    referenced = set(re.findall(r"(?i)(?:from|join)\s+dbo\.\[?([A-Za-z0-9_]+)\]?", s))
    referenced |= set(re.findall(r"(?i)(?:from|join)\s+\[?dbo\]?\.\[?([A-Za-z0-9_]+)\]?", s))
    referenced |= set(re.findall(r"(?i)(?:from|join)\s+\[?([A-Za-z0-9_]+)\]?", s))
    # "dbo" is the schema prefix in "dbo.table", not a table name.
    noise = {"select", "where", "and", "or", "on", "as", "top", "inner", "left", "right", "outer", "join", "dbo"}
    referenced = {t for t in referenced if t.lower() not in noise}
    unknown = [t for t in referenced if t not in tables and t.lower() not in {x.lower() for x in tables}]
    if unknown:
        return False, f"unknown tables: {unknown}"
    return True, "ok"


def generate_sql(question: str, schema: str, error: str | None = None, scope_hint: str | None = None) -> str:
    sys = (
        "You are an expert MSSQL Text-to-SQL assistant for FBR HRMS. "
        "Return ONLY one SQL Server SELECT query. Use dbo.table names. "
        "Prefer joins via EmployeeID to pr_employee_mf.ID. "
        "Use NICNo only when bridging identity. TOP clause for limits. "
        "When an AccessScope filter is provided, you MUST include it in the WHERE clause "
        "on the employee alias (typically e)."
    )
    user = f"Schema:\n{schema}\n\nQuestion: {question}\n"
    if scope_hint:
        user += f"\nMandatory AccessScope SQL filter (AND into employee WHERE):\n{scope_hint}\n"
    if error:
        user += f"\nPrevious SQL failed validation/execution: {error}\nFix the SQL."
    raw = chat_completion(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=1200,
    )
    return extract_sql(raw)


def _inject_scope_sql(sql: str, scope: AccessScope | None) -> tuple[str, dict[str, Any]]:
    """Append scope predicate when the query references pr_employee_mf and scope is restricted."""
    if scope is None or scope.level in (None, AccessID.FULL):
        return sql, {}
    pred, params = sql_scope_predicate(scope, alias="e")
    if pred in ("1=1", "1=0"):
        if pred == "1=0":
            # Force empty result safely
            return "SELECT TOP 0 * FROM dbo.pr_employee_mf e WHERE 1=0", {}
        return sql, {}
    # Only inject if employee table is present
    if not re.search(r"(?i)\bpr_employee_mf\b", sql):
        return sql, {}
    # Prefer alias e; if not present, skip injection and rely on post-filter
    if not re.search(r"(?i)\bpr_employee_mf\b\s+e\b", sql):
        return sql, params  # post-filter only
    if re.search(r"(?i)\bwhere\b", sql):
        sql = re.sub(r"(?i)\bwhere\b", f"WHERE ({pred}) AND ", sql, count=1)
    else:
        # before ORDER BY / GROUP BY / OPTION
        m = re.search(r"(?i)\b(order\s+by|group\s+by|option)\b", sql)
        if m:
            sql = sql[: m.start()] + f" WHERE ({pred}) " + sql[m.start() :]
        else:
            sql = sql + f" WHERE ({pred})"
    return sql, params


def ask_hrms(question: str, scope: AccessScope | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.allow_open_sql:
        return {
            "ok": False,
            "error": "open SQL disabled — use sanctioned/succession/promotion/training tools",
            "rows": [],
            "abstain_reason": "ALLOW_OPEN_SQL=0",
        }
    if scope is not None and (scope.denied or scope.level is None):
        return {"ok": False, "error": "access denied", "rows": [], "abstain_reason": "scope_denied"}
    # Production safer profile: only AccessID 0 (full) may run open Text-to-SQL
    if settings.is_production and scope is not None and scope.level != AccessID.FULL:
        return {
            "ok": False,
            "error": "open Text-to-SQL restricted to full access (AccessID 0) in production",
            "rows": [],
            "abstain_reason": "open_sql_requires_full_access",
        }

    cards = link_tables(question, top_k=8)
    schema = schema_pack(cards)
    scope_hint = None
    scope_params: dict[str, Any] = {}
    if scope is not None and scope.level not in (None, AccessID.FULL):
        pred, scope_params = sql_scope_predicate(scope, alias="e")
        scope_hint = pred

    sql = generate_sql(question, schema, scope_hint=scope_hint)
    attempts = []
    for i in range(settings.sql_max_repair):
        ok, msg = validate_sql(sql)
        attempts.append({"sql": sql, "valid": ok, "msg": msg})
        if not ok:
            cards = link_tables(question, top_k=8, widen=True)
            schema = schema_pack(cards)
            sql = generate_sql(question, schema, error=msg, scope_hint=scope_hint)
            continue
        try:
            capped = sql
            if re.match(r"(?is)^select\b", capped) and not re.search(r"(?is)^select\s+top\b", capped):
                capped = re.sub(r"(?is)^select\b", f"SELECT TOP {settings.sql_row_cap}", capped, count=1)
            capped, inj_params = _inject_scope_sql(capped, scope)
            params = {**scope_params, **inj_params}
            rows = hrms_execute(capped, params or None)
            # Defense-in-depth post-filter for employee-shaped rows
            if scope is not None and scope.level not in (None, AccessID.FULL):
                if rows and any(k in rows[0] for k in ("ID", "EmployeeID", "LocationID", "OrgLevel2")):
                    rows = filter_rows_by_scope(scope, rows)
            return {
                "ok": True,
                "sql": capped,
                "rows": rows[: settings.sql_row_cap],
                "tables": [c.name for c in cards],
                "attempts": attempts,
                "scoped": scope.access_id if scope else None,
            }
        except Exception as e:
            err = str(e)
            attempts[-1]["exec_error"] = err
            cards = link_tables(question, top_k=8, widen=True)
            schema = schema_pack(cards)
            sql = generate_sql(question, schema, error=err, scope_hint=scope_hint)
    return {
        "ok": False,
        "error": "abstain after repair cycles",
        "attempts": attempts,
        "candidate_tables": [c.name for c in cards],
        "rows": [],
    }
