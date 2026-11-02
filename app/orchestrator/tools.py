"""Multi-source agent tools — full HRMS / PF / Policy / rules surface."""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any

from app.auth.rbac import AccessID, AccessScope, can_recommend, employee_in_scope, filter_rows_by_scope, sql_scope_predicate
from app.core.config import get_settings
from app.db.hrms import hrms_execute
from app.orchestrator.tool_specs import TOOL_SPECS, tool_catalog_prompt
from app.rules import (
    eligibility_promotion_cadre,
    eligibility_promotion_excadre,
    eligibility_training_nomination,
    jd_gate,
    sanction_strength,
    succession_search,
)
from app.rules.status_map import load_status_map
from app.sources.hrms.catalog import link_tables, load_catalog, schema_pack
from app.sources.hrms.live_resolve import (
    completed_trainings_for,
    discipline_flags_from_pf,
    fetch_candidates_live,
    fetch_course_live,
    fetch_officer_live,
    fetch_seat_live,
    qualifying_service_years,
    resolve_grade_id,
    schedule_ii_lookup,
)
from app.sources.hrms.text2sql import _inject_scope_sql, validate_sql
from app.sources.personal_files.retrieve import (
    pf_events,
    pf_files,
    pf_list_categories,
    pf_pages,
    pf_profile_360,
    pf_resolve_officer,
    pf_search_for_query,
    pf_segments,
)
from app.sources.personal_files.vector_search import pf_vector_search
from app.sources.policies.retrieve import (
    jd_authority,
    list_policy_doc_types,
    list_policy_docs,
    list_schedule_ii,
    policy_corpus_stats,
    search_policies,
    search_policies_filtered,
)

log = logging.getLogger(__name__)

__all__ = ["TOOL_SPECS", "tool_catalog_prompt", "dispatch_tool"]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj) if obj % 1 else int(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    return obj


def _trim_rows(rows: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    return [_jsonable(r) for r in rows[:limit]]


def _tokens(q: str, stop: set[str] | None = None) -> list[str]:
    stop = stop or {"the", "and", "for", "of", "in", "at", "to", "a"}
    toks = [t for t in re.findall(r"[a-z0-9]{2,}", (q or "").lower()) if t not in stop]
    return toks or ([q.lower()] if q else [])


# —— HRMS entity search ——


def search_designations(query: str, limit: int = 25) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query required", "rows": []}
    tokens = _tokens(q)
    clauses = " AND ".join(f"LOWER(DesignationName) LIKE :t{i}" for i in range(len(tokens[:8])))
    params = {f"t{i}": f"%{tok}%" for i, tok in enumerate(tokens[:8])}
    rows = hrms_execute(
        f"""
        SELECT TOP {max(1, min(int(limit), 50))} ID, DesignationName, SanctionedStrength
        FROM dbo.pr_designation WHERE {clauses}
        ORDER BY LEN(DesignationName), DesignationName
        """,
        params,
    )
    return {"ok": True, "rows": _trim_rows(rows), "tokens": tokens}


def search_locations(query: str, limit: int = 30) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query required", "rows": []}
    tokens = _tokens(q)
    clauses = " AND ".join(f"LOWER(LocationName) LIKE :t{i}" for i in range(len(tokens[:8])))
    params = {f"t{i}": f"%{tok}%" for i, tok in enumerate(tokens[:8])}
    rows = hrms_execute(
        f"""
        SELECT TOP {max(1, min(int(limit), 60))} ID, LocationName, CityID, Address
        FROM dbo.adm_company_location WHERE {clauses}
        ORDER BY LEN(LocationName), LocationName
        """,
        params,
    )
    return {"ok": True, "rows": _trim_rows(rows), "tokens": tokens}


def search_org_levels(query: str, limit: int = 20) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        rows = hrms_execute(
            """
            SELECT TOP 20 ID, Name, Code, Type, IsActive FROM dbo.adm_org_structure
            WHERE Type IN (1, 2) OR ID IN (11, 12) ORDER BY ID
            """
        )
        return {"ok": True, "rows": _trim_rows(rows), "note": "top-level entities"}
    if q.upper() in ("IRS", "PCS"):
        rows = hrms_execute(
            "SELECT ID, Name, Code, Type, IsActive FROM dbo.adm_org_structure WHERE ID IN (11, 12)"
        )
        return {"ok": True, "rows": _trim_rows(rows)}
    rows = hrms_execute(
        """
        SELECT TOP 40 ID, Name, Code, Type, IsActive FROM dbo.adm_org_structure
        WHERE LOWER(Name) LIKE :q OR LOWER(ISNULL(Code,'')) LIKE :q
        ORDER BY Type, LEN(Name)
        """,
        {"q": f"%{q.lower()}%"},
    )
    return {"ok": True, "rows": _trim_rows(rows[: max(1, min(int(limit), 40))])}


def search_grades(query: str, limit: int = 25) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query required", "rows": []}
    rows = hrms_execute(
        f"""
        SELECT TOP {max(1, min(int(limit), 40))} ID, GradeName, GradeCode, SanctionedStrength
        FROM dbo.pr_employee_grades_mf
        WHERE LOWER(ISNULL(GradeName,'')) LIKE :q
           OR LOWER(ISNULL(GradeCode,'')) LIKE :q
        ORDER BY ID
        """,
        {"q": f"%{q.lower()}%"},
    )
    return {"ok": True, "rows": _trim_rows(rows)}


def search_positions(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 40), 80))
    query = (args.get("query") or args.get("position_query") or "").strip()
    desig_ids = _pick_designation_ids(args.get("designation_query"), args.get("designation_id"))
    loc_ids = _pick_location_ids(args.get("location_query"), args.get("location_id"))
    org_level2 = _pick_org_level2(args.get("org_query"), args.get("org_level2"))
    grade_id = args.get("grade_id")
    if grade_id is None and args.get("grade_query"):
        g = search_grades(str(args["grade_query"]), limit=5)
        if g.get("rows"):
            grade_id = int(g["rows"][0]["ID"])

    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if query:
        tokens = _tokens(query)[:6]
        for i, tok in enumerate(tokens):
            params[f"pq{i}"] = f"%{tok}%"
            clauses.append(f"LOWER(ISNULL(p.PositionName,'')) LIKE :pq{i}")
    if desig_ids:
        ph = []
        for i, d in enumerate(desig_ids[:10]):
            params[f"d{i}"] = d
            ph.append(f":d{i}")
        clauses.append(f"p.DesignationID IN ({', '.join(ph)})")
    if loc_ids:
        ph = []
        for i, lid in enumerate(loc_ids[:40]):
            params[f"l{i}"] = lid
            ph.append(f":l{i}")
        clauses.append(f"p.LocationID IN ({', '.join(ph)})")
    if org_level2 is not None:
        clauses.append("p.OrgLevel2 = :o2")
        params["o2"] = org_level2
    if grade_id is not None:
        clauses.append("p.GradeID = :gid")
        params["gid"] = int(grade_id)

    holding = load_status_map().get("holding_occupy_ids") or get_settings().holding_status_ids
    hold_csv = ", ".join(str(int(x)) for x in holding) or "-1"
    where = " AND ".join(clauses)
    rows = hrms_execute(
        f"""
        SELECT TOP {limit}
          p.ID, p.PositionName, p.GradeID, p.DesignationID, p.LocationID, p.OrgLevel2,
          d.DesignationName, l.LocationName, g.GradeCode, g.GradeName,
          (
            SELECT COUNT(*) FROM dbo.pr_employee_mf e
            WHERE e.PositionID = p.ID AND e.StatusID IN ({hold_csv})
          ) AS Occupants
        FROM dbo.pr_position_mf p
        LEFT JOIN dbo.pr_designation d ON d.ID = p.DesignationID
        LEFT JOIN dbo.adm_company_location l ON l.ID = p.LocationID
        LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = p.GradeID
        WHERE {where}
        ORDER BY
          CASE WHEN (
            SELECT COUNT(*) FROM dbo.pr_employee_mf e
            WHERE e.PositionID = p.ID AND e.StatusID IN ({hold_csv})
          ) = 0 THEN 0 ELSE 1 END,
          p.ID
        """,
        params,
    )
    if args.get("vacant_only"):
        rows = [r for r in rows if int(r.get("Occupants") or 0) == 0]
    for r in rows:
        r["vacant"] = int(r.get("Occupants") or 0) == 0
    return {
        "ok": True,
        "rows": _trim_rows(rows),
        "filters": {
            "designation_ids": desig_ids,
            "location_ids": loc_ids[:15],
            "org_level2": org_level2,
            "grade_id": grade_id,
            "query": query or None,
        },
        "count": len(rows),
    }


def describe_table(table: str) -> dict[str, Any]:
    name = (table or "").strip()
    if name.lower().startswith("dbo."):
        name = name[4:]
    cat = load_catalog()
    meta = (cat.get("tables") or {}).get(name)
    if not meta:
        # case-insensitive
        for k, v in (cat.get("tables") or {}).items():
            if k.lower() == name.lower():
                meta = v
                name = k
                break
    if not meta:
        return {"ok": False, "error": f"table not in catalog: {table}"}
    return {
        "ok": True,
        "table": name,
        "columns": meta.get("columns") or [],
        "pk": meta.get("pk") or [],
        "fks": meta.get("fks") or [],
        "description": meta.get("description") or "",
        "in_scope": meta.get("in_scope"),
    }


def _pick_designation_ids(designation_query: str | None, designation_id: int | None) -> list[int]:
    if designation_id is not None:
        return [int(designation_id)]
    if not designation_query:
        return []
    found = search_designations(designation_query, limit=15)
    ids: list[int] = []
    qn = designation_query.strip().lower()
    for r in found.get("rows") or []:
        name = str(r.get("DesignationName") or "").strip().lower()
        if name == qn or name.rstrip() == qn:
            ids.insert(0, int(r["ID"]))
        else:
            ids.append(int(r["ID"]))
    out: list[int] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out[:8]


def _pick_location_ids(location_query: str | None, location_id: int | None) -> list[int]:
    if location_id is not None:
        return [int(location_id)]
    if not location_query:
        return []
    found = search_locations(location_query, limit=40)
    return [int(r["ID"]) for r in (found.get("rows") or []) if r.get("ID") is not None][:40]


def _pick_org_level2(org_query: str | None, org_level2: int | None) -> int | None:
    if org_level2 is not None:
        return int(org_level2)
    if not org_query:
        return None
    u = org_query.strip().upper()
    if u in ("IRS", "INLAND REVENUE", "INLAND"):
        return 11
    if u in ("PCS", "CUSTOMS", "PAKISTAN CUSTOMS"):
        return 12
    found = search_org_levels(org_query, limit=10)
    rows = found.get("rows") or []
    for r in rows:
        if int(r.get("Type") or 0) == 2 or int(r.get("ID") or 0) in (11, 12):
            return int(r["ID"])
    return int(rows[0]["ID"]) if rows else None


def list_sanction_allocations(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 50), 100))
    desig_ids = _pick_designation_ids(args.get("designation_query"), args.get("designation_id"))
    loc_ids = _pick_location_ids(args.get("location_query"), args.get("location_id"))
    org_level2 = _pick_org_level2(args.get("org_query"), args.get("org_level2"))
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if desig_ids:
        ph = []
        for i, d in enumerate(desig_ids[:10]):
            params[f"d{i}"] = d
            ph.append(f":d{i}")
        clauses.append(f"a.DesignationID IN ({', '.join(ph)})")
    if loc_ids:
        ph = []
        for i, lid in enumerate(loc_ids[:40]):
            params[f"l{i}"] = lid
            ph.append(f":l{i}")
        clauses.append(f"a.LocationID IN ({', '.join(ph)})")
    elif args.get("location_query"):
        params["loc"] = f"%{str(args['location_query']).lower()}%"
        clauses.append("LOWER(l.LocationName) LIKE :loc")
    if org_level2 is not None:
        clauses.append("a.OrgLevel2 = :o2")
        params["o2"] = org_level2
    rows = hrms_execute(
        f"""
        SELECT TOP {limit}
          a.ID, a.DesignationID, d.DesignationName, a.OrgLevel2, o.Name AS OrgName,
          a.LocationID, l.LocationName, a.SanctionedStrength
        FROM dbo.pr_designation_sanctioned_allocation a
        JOIN dbo.pr_designation d ON d.ID = a.DesignationID
        LEFT JOIN dbo.adm_company_location l ON l.ID = a.LocationID
        LEFT JOIN dbo.adm_org_structure o ON o.ID = a.OrgLevel2
        WHERE {' AND '.join(clauses)}
        ORDER BY a.SanctionedStrength DESC, l.LocationName
        """,
        params,
    )
    total = float(sum(float(r.get("SanctionedStrength") or 0) for r in rows))
    return {"ok": True, "rows": _trim_rows(rows), "total_sanctioned": total, "count": len(rows)}


def tool_sanction_strength(args: dict[str, Any], scope: AccessScope) -> dict[str, Any]:
    lens = (args.get("lens") or "designation").lower()
    if lens not in ("grade", "designation", "position"):
        lens = "designation"

    grade_id = args.get("grade_id")
    if grade_id is None and args.get("grade_query"):
        g = search_grades(str(args["grade_query"]), limit=5)
        if g.get("rows"):
            grade_id = int(g["rows"][0]["ID"])

    designation_id = args.get("designation_id")
    location_id = args.get("location_id")
    org_level2 = _pick_org_level2(args.get("org_query"), args.get("org_level2"))
    desig_ids = _pick_designation_ids(args.get("designation_query"), designation_id)
    loc_ids = _pick_location_ids(args.get("location_query"), location_id)
    loc_q = args.get("location_query")

    # Designation lens with location/org filters → allocation table sum
    dids = desig_ids or ([int(designation_id)] if designation_id is not None else [])
    if lens == "designation" and dids and (loc_ids or loc_q or org_level2 is not None):
        params: dict[str, Any] = {}
        ph = []
        for i, d in enumerate(dids[:10]):
            params[f"d{i}"] = d
            ph.append(f":d{i}")
        clauses = [f"a.DesignationID IN ({', '.join(ph)})"]
        if org_level2 is not None:
            clauses.append("a.OrgLevel2 = :o2")
            params["o2"] = org_level2
        if loc_ids:
            lph = []
            for i, lid in enumerate(loc_ids[:40]):
                params[f"l{i}"] = lid
                lph.append(f":l{i}")
            clauses.append(f"a.LocationID IN ({', '.join(lph)})")
        elif loc_q:
            params["loc"] = f"%{str(loc_q).lower()}%"
            clauses.append("LOWER(l.LocationName) LIKE :loc")
        rows = hrms_execute(
            f"""
            SELECT a.DesignationID, d.DesignationName, a.OrgLevel2, o.Name AS OrgName,
                   a.LocationID, l.LocationName, a.SanctionedStrength
            FROM dbo.pr_designation_sanctioned_allocation a
            JOIN dbo.pr_designation d ON d.ID = a.DesignationID
            LEFT JOIN dbo.adm_company_location l ON l.ID = a.LocationID
            LEFT JOIN dbo.adm_org_structure o ON o.ID = a.OrgLevel2
            WHERE {' AND '.join(clauses)}
            """,
            params,
        )
        sanctioned = float(sum(float(r.get("SanctionedStrength") or 0) for r in rows))
        occupied = 0
        occupied_sample: list[dict[str, Any]] = []
        if rows:
            holding = load_status_map().get("holding_occupy_ids") or get_settings().holding_status_ids
            hold_csv = ", ".join(str(int(x)) for x in holding) or "-1"
            loc_set = list({int(r["LocationID"]) for r in rows if r.get("LocationID") is not None})
            did_set = list({int(r["DesignationID"]) for r in rows})
            if did_set and loc_set:
                op: dict[str, Any] = {}
                d_ph = [f":d{i}" for i, d in enumerate(did_set)]
                for i, d in enumerate(did_set):
                    op[f"d{i}"] = d
                l_ph = [f":l{i}" for i, lid in enumerate(loc_set)]
                for i, lid in enumerate(loc_set):
                    op[f"l{i}"] = lid
                occ_sql = f"""
                    SELECT e.ID, e.FirstName, e.LastName, e.StatusID, e.LocationID,
                           e.WorkingLocationID, e.DesignationID, e.OrgLevel2
                    FROM dbo.pr_employee_mf e
                    WHERE e.StatusID IN ({hold_csv})
                      AND e.DesignationID IN ({', '.join(d_ph)})
                      AND (e.LocationID IN ({', '.join(l_ph)}) OR e.WorkingLocationID IN ({', '.join(l_ph)}))
                """
                if org_level2 is not None:
                    occ_sql += " AND e.OrgLevel2 = :o2"
                    op["o2"] = org_level2
                occ_rows = hrms_execute(occ_sql, op)
                occupied = len(occ_rows)
                occupied_sample = _trim_rows(occ_rows, 15)
        return {
            "ok": True,
            "lens": "designation",
            "sanctioned": sanctioned,
            "occupied": occupied,
            "vacant": sanctioned - occupied,
            "allocations": _trim_rows(rows),
            "resolved": {"designation_ids": dids, "location_ids": loc_ids[:20], "org_level2": org_level2},
            "occupied_sample": occupied_sample,
        }

    # Position lens: count positions (+ occupancy via Occupants)
    if lens == "position":
        pos_args = {
            "query": args.get("position_query") or args.get("query"),
            "designation_query": args.get("designation_query"),
            "designation_id": designation_id or (desig_ids[0] if desig_ids else None),
            "location_query": loc_q,
            "location_id": location_id,
            "org_query": args.get("org_query"),
            "org_level2": org_level2,
            "grade_id": grade_id,
            "grade_query": args.get("grade_query"),
            "limit": 80,
        }
        if args.get("position_id") is not None:
            rows = hrms_execute(
                """
                SELECT p.ID, p.PositionName, p.GradeID, p.DesignationID, p.LocationID, p.OrgLevel2,
                       d.DesignationName, l.LocationName
                FROM dbo.pr_position_mf p
                LEFT JOIN dbo.pr_designation d ON d.ID = p.DesignationID
                LEFT JOIN dbo.adm_company_location l ON l.ID = p.LocationID
                WHERE p.ID = :pid
                """,
                {"pid": int(args["position_id"])},
            )
            pos = {"ok": True, "rows": _trim_rows(rows), "count": len(rows)}
        else:
            pos = search_positions(pos_args)
        rows = pos.get("rows") or []
        sanctioned = float(len(rows))
        occupied = float(sum(int(r.get("Occupants") or 0) for r in rows))
        vacant = float(sum(1 for r in rows if r.get("vacant") or int(r.get("Occupants") or 0) == 0))
        return {
            "ok": True,
            "lens": "position",
            "sanctioned": sanctioned,
            "occupied": occupied,
            "vacant_positions": vacant,
            "vacant": vacant,
            "positions": rows[:40],
            "filters": pos.get("filters"),
        }

    # Grade / single-ID fallback via rule engine
    if designation_id is None and desig_ids:
        designation_id = desig_ids[0]
    if location_id is None and loc_ids:
        location_id = loc_ids[0]
    if grade_id is None and lens == "grade":
        return {"ok": False, "error": "grade_id or grade_query required", "status": "MISSING_INPUTS"}
    rr = sanction_strength(
        lens,  # type: ignore[arg-type]
        grade_id=int(grade_id) if grade_id is not None else None,
        designation_id=int(designation_id) if designation_id is not None else None,
        location_id=int(location_id) if location_id is not None else None,
        org_level2=int(org_level2) if org_level2 is not None else None,
    )
    out = rr.to_dict()
    out["ok"] = rr.status.value not in ("MISSING_INPUTS",)
    return _jsonable(out)


def run_hrms_sql(sql: str, scope: AccessScope) -> dict[str, Any]:
    settings = get_settings()
    if not settings.allow_open_sql and not settings.agent_allow_sql:
        return {"ok": False, "error": "SQL tools disabled", "rows": []}
    if scope.denied or scope.level is None:
        return {"ok": False, "error": "access denied", "rows": []}
    ok, msg = validate_sql(sql)
    if not ok:
        return {"ok": False, "error": msg, "rows": []}
    capped = sql.strip().rstrip(";")
    if re.match(r"(?is)^select\b", capped) and not re.search(r"(?is)^select\s+top\b", capped):
        capped = re.sub(r"(?is)^select\b", f"SELECT TOP {settings.sql_row_cap}", capped, count=1)
    capped, inj = _inject_scope_sql(capped, scope)
    scope_params: dict[str, Any] = {}
    if scope.level not in (None, AccessID.FULL):
        _, scope_params = sql_scope_predicate(scope, alias="e")
    params = {**scope_params, **inj}
    try:
        rows = hrms_execute(capped, params or None)
        if scope.level not in (None, AccessID.FULL):
            if rows and any(k in rows[0] for k in ("ID", "EmployeeID", "LocationID", "OrgLevel2")):
                rows = filter_rows_by_scope(scope, rows)
        return {"ok": True, "sql": capped, "rows": _trim_rows(rows, settings.sql_row_cap), "row_count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e), "sql": capped, "rows": []}


# —— PF helpers ——


def _pf_scope_gate(scope: AccessScope, resolved: dict[str, Any]) -> dict[str, Any] | None:
    if scope and resolved.get("hrms_row"):
        if not employee_in_scope(scope, resolved["hrms_row"]):
            return {"ok": False, "denied": True, "error": "Officer outside AccessScope"}
    if scope and scope.access_id == 3 and scope.employee_id is not None:
        hid = resolved.get("hrms_employee_id")
        if hid is not None and int(hid) != int(scope.employee_id):
            return {"ok": False, "denied": True, "error": "Self scope: own PF only"}
    return None


def _pf_resolve_args(args: dict[str, Any], scope: AccessScope) -> dict[str, Any]:
    return pf_resolve_officer(
        cnic=args.get("cnic"),
        name=args.get("name") or args.get("employee_name"),
        employee_id=args.get("employee_id") or (scope.employee_id if scope.access_id == 3 else None),
    )


def _pf_cnics_or_error(args: dict[str, Any], scope: AccessScope) -> tuple[list[str] | None, dict[str, Any] | None]:
    resolved = _pf_resolve_args(args, scope)
    gate = _pf_scope_gate(scope, resolved)
    if gate:
        return None, gate
    cnics = list(resolved.get("cnics") or [])
    if not cnics:
        return None, {"ok": False, "error": "No CNIC resolved", "resolved": _jsonable(resolved)}
    if not resolved.get("pf_exists"):
        return None, {"ok": False, "error": "No Personal File in OCR", "resolved": _jsonable(resolved)}
    return cnics, {"resolved": _jsonable(resolved)}


# —— Dispatch ——


def dispatch_tool(
    name: str,
    args: dict[str, Any],
    *,
    scope: AccessScope,
    question: str,
) -> dict[str, Any]:
    args = args or {}
    try:
        if name == "search_catalog":
            cards = link_tables(str(args.get("query") or question), top_k=int(args.get("top_k") or 8))
            return {
                "ok": True,
                "tables": [
                    {"name": c.name, "columns": c.columns[:30], "description": c.description} for c in cards
                ],
                "schema_pack": schema_pack(cards)[:5000],
            }
        if name == "describe_table":
            return describe_table(str(args.get("table") or ""))
        if name == "search_designations":
            return search_designations(str(args.get("query") or ""), limit=int(args.get("limit") or 25))
        if name == "search_locations":
            return search_locations(str(args.get("query") or ""), limit=int(args.get("limit") or 30))
        if name == "search_org_levels":
            return search_org_levels(str(args.get("query") or ""), limit=int(args.get("limit") or 20))
        if name == "search_grades":
            return search_grades(str(args.get("query") or ""), limit=int(args.get("limit") or 25))
        if name == "search_positions":
            return search_positions(args)
        if name == "list_sanction_allocations":
            return list_sanction_allocations(args)
        if name == "run_hrms_sql":
            return run_hrms_sql(str(args.get("sql") or ""), scope)
        if name == "sanction_strength":
            return tool_sanction_strength(args, scope)
        if name == "resolve_officer":
            off = fetch_officer_live(
                cnic=args.get("cnic"),
                name=args.get("name") or args.get("employee_name"),
                employee_id=args.get("employee_id")
                or (scope.employee_id if scope.access_id == 3 else None),
                scope=scope,
            )
            return {"ok": bool(off), "officer": _jsonable(off)}

        # PF facets
        if name == "pf_resolve":
            resolved = _pf_resolve_args(args, scope)
            gate = _pf_scope_gate(scope, resolved)
            if gate:
                return gate
            return {"ok": True, "resolved": _jsonable(resolved)}
        if name == "pf_categories":
            cnics, meta = _pf_cnics_or_error(args, scope)
            if cnics is None:
                return meta  # type: ignore[return-value]
            return {"ok": True, **meta, "categories": _jsonable(pf_list_categories(cnics))}  # type: ignore[misc]
        if name == "pf_pages":
            cnics, meta = _pf_cnics_or_error(args, scope)
            if cnics is None:
                return meta  # type: ignore[return-value]
            topics = args.get("topics") if isinstance(args.get("topics"), list) else None
            rows = pf_pages(
                cnics,
                topics=topics,
                query_text=args.get("query_text") or args.get("question"),
                limit=int(args.get("limit") or 25),
            )
            return {"ok": True, **meta, "pages": _jsonable(rows)}  # type: ignore[misc]
        if name == "pf_events":
            cnics, meta = _pf_cnics_or_error(args, scope)
            if cnics is None:
                return meta  # type: ignore[return-value]
            topics = args.get("topics") if isinstance(args.get("topics"), list) else None
            return {
                "ok": True,
                **meta,  # type: ignore[misc]
                "events": _jsonable(pf_events(cnics, topics=topics, limit=int(args.get("limit") or 40))),
            }
        if name == "pf_segments":
            cnics, meta = _pf_cnics_or_error(args, scope)
            if cnics is None:
                return meta  # type: ignore[return-value]
            topics = args.get("topics") if isinstance(args.get("topics"), list) else None
            return {
                "ok": True,
                **meta,  # type: ignore[misc]
                "segments": _jsonable(pf_segments(cnics, topics=topics, limit=int(args.get("limit") or 25))),
            }
        if name == "pf_files":
            cnics, meta = _pf_cnics_or_error(args, scope)
            if cnics is None:
                return meta  # type: ignore[return-value]
            return {
                "ok": True,
                **meta,  # type: ignore[misc]
                "files": _jsonable(pf_files(cnics, limit=int(args.get("limit") or 20))),
            }
        if name == "pf_profile":
            cnics, meta = _pf_cnics_or_error(args, scope)
            if cnics is None:
                return meta  # type: ignore[return-value]
            return {"ok": True, **meta, "profile": _jsonable(pf_profile_360(cnics, scope))}  # type: ignore[misc]
        if name == "pf_vector_search":
            cnics_list = None
            resolved = _pf_resolve_args(args, scope) if (args.get("cnic") or args.get("name")) else None
            if resolved:
                gate = _pf_scope_gate(scope, resolved)
                if gate:
                    return gate
                cnics_list = list(resolved.get("cnics") or [])
            return _jsonable(
                pf_vector_search(
                    str(args.get("question") or question),
                    cnics=cnics_list,
                    top_k=int(args.get("top_k") or 12),
                )
            )
        if name in ("pf_search", "search_personal_files"):
            return _jsonable(
                pf_search_for_query(
                    str(args.get("question") or question),
                    cnic=args.get("cnic"),
                    name=args.get("employee_name") or args.get("name"),
                    employee_id=scope.employee_id if scope.access_id == 3 else None,
                    scope=scope,
                )
            )

        # Policy corpus
        if name == "search_policies":
            q = str(args.get("question") or question)
            if any(args.get(k) for k in ("doc_type", "office_type", "jd_only", "doc_name_contains")):
                hits = search_policies_filtered(
                    q,
                    top_k=int(args.get("top_k") or 8),
                    doc_type=args.get("doc_type"),
                    office_type=args.get("office_type"),
                    jd_only=bool(args.get("jd_only")),
                    doc_name_contains=args.get("doc_name_contains"),
                )
            else:
                hits = search_policies(q, top_k=int(args.get("top_k") or 8))
            return {"ok": True, "hits": _jsonable(hits)}
        if name == "list_policy_docs":
            return {"ok": True, "docs": _jsonable(list_policy_docs(limit=int(args.get("limit") or 80)))}
        if name == "list_policy_doc_types":
            return {"ok": True, "doc_types": _jsonable(list_policy_doc_types())}
        if name == "policy_corpus_stats":
            return {"ok": True, "stats": _jsonable(policy_corpus_stats())}
        if name == "jd_authority":
            return {"ok": True, "authority": _jsonable(jd_authority())}
        if name == "schedule_ii":
            return _jsonable(schedule_ii_lookup(int(args["from_bps"]), int(args["to_bps"])))
        if name == "list_schedule_ii":
            return {"ok": True, "rows": _jsonable(list_schedule_ii(limit=int(args.get("limit") or 100)))}

        # Rules
        if name == "rule_promotion":
            return _rule_promotion(args, scope)
        if name == "rule_succession":
            return _rule_succession(args, scope, question)
        if name == "rule_training":
            return _rule_training(args, scope, question)
        if name == "rule_jd":
            return _rule_jd(args, scope, question)
        if name == "finish":
            return {
                "ok": True,
                "finished": True,
                "answer_hint": args.get("answer_hint") or "",
                "topic": args.get("topic") or "general",
            }
        return {"ok": False, "error": f"unknown tool {name}"}
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"ok": False, "error": str(e), "tool": name}


def _rule_promotion(args: dict[str, Any], scope: AccessScope) -> dict[str, Any]:
    officer = fetch_officer_live(
        cnic=args.get("cnic"),
        name=args.get("employee_name"),
        employee_id=scope.employee_id
        if (scope.access_id == 3 or not (args.get("cnic") or args.get("employee_name")))
        else None,
        scope=scope,
    )
    if not officer:
        return {"ok": False, "status": "MISSING_INPUTS", "reasons": ["Officer identity required"]}
    current = officer.get("bps")
    target = args.get("target_bps")
    if target is None and current is not None:
        target = int(current) + 1
    if target is None:
        return {"ok": False, "status": "MISSING_INPUTS", "reasons": ["target BPS unknown"]}
    flags = discipline_flags_from_pf(officer.get("cnics") or [])
    trainings = completed_trainings_for(int(officer["ID"])) if officer.get("ID") else []
    svc = qualifying_service_years(int(officer["ID"])) if officer.get("ID") else None
    sched = schedule_ii_lookup(int(current), int(target))
    required = sched.get("min_years")
    if officer.get("is_excadre") or args.get("from_designation"):
        rr = eligibility_promotion_excadre(
            officer,
            from_designation=args.get("from_designation") or officer.get("designation_title") or "",
            to_designation=args.get("to_designation") or "",
            designation_id=args.get("designation_id") or officer.get("DesignationID"),
        )
    else:
        tg = resolve_grade_id(bps=int(target))
        if tg:
            officer = {**officer, "target_grade_id": int(tg["ID"])}
        rr = eligibility_promotion_cadre(
            officer,
            target_bps=int(target),
            completed_trainings=trainings,
            qualifying_service_years_bs17=svc,
            required_service_years=required,
            open_ed_accused=flags.get("open_ed", False),
            open_inquiry=flags.get("open_inquiry", False),
            covering_block=flags.get("covering_suspension", False),
            schedule_ii_ok=True if sched.get("status") == "ok" else None,
        )
    return _jsonable(
        {
            "ok": True,
            "promotion": rr.to_dict(),
            "officer": {
                "id": officer.get("ID"),
                "name": officer.get("name"),
                "bps": current,
                "is_excadre": officer.get("is_excadre"),
            },
            "schedule_ii": sched,
            "trainings": trainings,
            "discipline": flags,
        }
    )


def _rule_succession(args: dict[str, Any], scope: AccessScope, question: str) -> dict[str, Any]:
    ok, reason = can_recommend(scope)
    if not ok:
        return {"ok": False, "status": "BLOCK", "reasons": [reason]}
    seat = fetch_seat_live(
        position_id=args.get("position_id"),
        grade_id=args.get("grade_id"),
        designation_id=args.get("designation_id"),
        question=str(args.get("question") or question),
    )
    if not seat and args.get("position_query"):
        pos = search_positions({"query": args["position_query"], "limit": 5, "vacant_only": True})
        if pos.get("rows"):
            seat = fetch_seat_live(position_id=int(pos["rows"][0]["ID"]))
    if not seat:
        g = resolve_grade_id(question=str(args.get("question") or question), grade_id=args.get("grade_id"))
        if g:
            seat = fetch_seat_live(grade_id=int(g["ID"]), question=question)
    if not seat:
        return {"ok": False, "status": "MISSING_INPUTS", "reasons": ["No seat resolved"]}
    candidates = fetch_candidates_live(seat, scope=scope, limit=40)
    rr = succession_search(seat=seat, candidates=candidates, scope=scope, seat_location_id=seat.get("LocationID"))
    return _jsonable(
        {
            "ok": True,
            "succession": rr.to_dict(),
            "seat": {
                k: seat.get(k) for k in ("ID", "PositionName", "bps", "GradeID", "vacant", "LocationID")
            },
            "candidates_considered": len(candidates),
        }
    )


def _rule_training(args: dict[str, Any], scope: AccessScope, question: str) -> dict[str, Any]:
    officer = fetch_officer_live(
        cnic=args.get("cnic"),
        name=args.get("employee_name"),
        employee_id=scope.employee_id if scope.access_id == 3 else None,
        scope=scope,
    )
    course = fetch_course_live(question=str(args.get("question") or question), course_name=args.get("course_name"))
    if not officer:
        return {"ok": False, "status": "MISSING_INPUTS", "reasons": ["Officer required"]}
    if not course:
        return {"ok": False, "status": "MISSING_INPUTS", "reasons": ["Course required"]}
    prior = completed_trainings_for(int(officer["ID"])) if officer.get("ID") else []
    already = any(
        str(course.get("CourseName") or "").upper() in t or t in str(course.get("CourseName") or "").upper()
        for t in prior
    )
    rr = eligibility_training_nomination(officer, course, prior_class3_certs=prior, already_completed=already)
    return _jsonable({"ok": True, "training": rr.to_dict(), "course": course, "prior": prior[:20]})


def _rule_jd(args: dict[str, Any], scope: AccessScope, question: str) -> dict[str, Any]:
    policy_res = search_policies_filtered(
        str(args.get("question") or question), top_k=6, jd_only=True
    ) or search_policies(str(args.get("question") or question), top_k=6)
    officer = (
        fetch_officer_live(
            cnic=args.get("cnic"),
            name=args.get("employee_name"),
            employee_id=scope.employee_id,
            scope=scope,
        )
        or {}
    )
    office = args.get("office_type") or ("HQ" if "hq" in question.lower() else "Field")
    jd_text = None
    jd_doc_id = "hq_feb_2022" if office == "HQ" else "field_june_2022"
    for p in policy_res:
        if p.get("is_jd_book") or p.get("doc_type") == "jd":
            jd_text = p.get("text")
            name = (p.get("doc_name") or "").lower()
            if "hq" in name:
                jd_doc_id = "hq_feb_2022"
            elif "field" in name:
                jd_doc_id = "field_june_2022"
            break
    seat = fetch_seat_live(question=question) or {}
    trainings = completed_trainings_for(int(officer["ID"])) if officer.get("ID") else []
    officer_j = {**officer, "trainings": trainings}
    rr = jd_gate(
        office_type=office,
        jd_doc_id=jd_doc_id,
        officer=officer_j,
        seat=seat,
        jd_text=jd_text,
        profile_text=str(officer_j.get("name") or "") + f" BPS-{officer_j.get('bps')}",
        run_soft_fit=bool(jd_text and officer),
    )
    return _jsonable({"ok": True, "jd": rr.to_dict(), "policy_hits": policy_res[:4]})
