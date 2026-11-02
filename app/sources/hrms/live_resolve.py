"""Live HRMS resolvers — officers, seats, candidates, grades, trainings.

No hardcoded demo officers/seats. All data comes from SQL Server HRMS
(and OCR for discipline signals when available).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from app.auth.rbac import AccessScope, employee_in_scope
from app.core.config import get_settings
from app.db.hrms import hrms_execute
from app.db.ocr import ocr_execute
from app.rules.status_map import load_status_map
from app.services.entity_resolution import cnic_digits, cnic_variants, resolve_officer

log = logging.getLogger(__name__)

_BPS_RE = re.compile(r"(?:BS|BPS)[-\s]?(\d{1,2})", re.I)


def parse_bps(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if 1 <= n <= 22 else None
    s = str(raw).strip()
    m = _BPS_RE.search(s)
    if m:
        return int(m.group(1))
    digits = "".join(c for c in s if c.isdigit())
    if digits and 1 <= int(digits) <= 22:
        return int(digits)
    return None


def infer_service(row: dict[str, Any]) -> str | None:
    """Prefer authoritative HRMS name fields; designation title is next-best signal."""
    blobs: list[str] = []
    for key in (
        "ServiceName",
        "CadreName",
        "service",
        "ServiceCode",
        "GroupName",
        "EmployeeGroupName",
        "DesignationName",
        "designation_title",
        "PositionName",
        "WorkingDesignation",
    ):
        v = row.get(key)
        if v:
            blobs.append(str(v))
    joined = " | ".join(blobs).upper()
    if not joined:
        return None
    if "IRS" in joined or "INLAND REVENUE" in joined:
        return "IRS"
    if "PCS" in joined or "PAKISTAN CUSTOMS" in joined or "CUSTOMS" in joined:
        return "PCS"
    if "DRS" in joined:
        return "DRS"
    if "BOARD" in joined and ("FBR" in joined or "REVENUE" in joined):
        return "BOARD"
    return None


def infer_is_excadre(row: dict[str, Any]) -> bool:
    """True when HRMS flag or designation/title clearly marks Ex-Cadre."""
    for key in ("IsExCadre", "is_excadre"):
        v = row.get(key)
        if v is True or v == 1 or str(v).lower() in {"1", "true", "yes"}:
            return True
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("DesignationName", "designation_title", "PositionName", "EmployeeGroupName", "CadreName")
    ).lower()
    if "ex-cadre" in blob or "ex cadre" in blob or "excadre" in blob:
        return True
    return False


def resolve_grade_id(question: str | None = None, grade_id: int | None = None, bps: int | None = None) -> dict[str, Any] | None:
    if grade_id is not None:
        rows = hrms_execute(
            """
            SELECT TOP 1 ID, GradeName, GradeCode, SanctionedStrength
            FROM dbo.pr_employee_grades_mf WHERE ID = :gid
            """,
            {"gid": grade_id},
        )
        return rows[0] if rows else None

    target_bps = bps
    if target_bps is None and question:
        m = _BPS_RE.search(question)
        if m:
            target_bps = int(m.group(1))
    if target_bps is None:
        return None

    rows = hrms_execute(
        """
        SELECT TOP 5 ID, GradeName, GradeCode, SanctionedStrength
        FROM dbo.pr_employee_grades_mf
        WHERE GradeCode LIKE :p OR GradeName LIKE :p2 OR GradeName LIKE :p3
        ORDER BY ID
        """,
        {
            "p": f"%{target_bps}%",
            "p2": f"%BS-{target_bps}%",
            "p3": f"%BPS-{target_bps}%",
        },
    )
    if not rows:
        # fallback: scan small set
        allg = hrms_execute(
            "SELECT TOP 80 ID, GradeName, GradeCode, SanctionedStrength FROM dbo.pr_employee_grades_mf ORDER BY ID"
        )
        for r in allg:
            if parse_bps(r.get("GradeCode")) == target_bps or parse_bps(r.get("GradeName")) == target_bps:
                return r
        return None
    for r in rows:
        if parse_bps(r.get("GradeCode")) == target_bps or parse_bps(r.get("GradeName")) == target_bps:
            return r
    return rows[0]


def resolve_designation_id(question: str | None = None, designation_id: int | None = None) -> dict[str, Any] | None:
    if designation_id is not None:
        rows = hrms_execute(
            "SELECT TOP 1 ID, DesignationName, SanctionedStrength FROM dbo.pr_designation WHERE ID = :did",
            {"did": designation_id},
        )
        return rows[0] if rows else None
    if not question:
        return None
    # Extract quoted title or known keywords after "designation"
    q = question.strip()
    m = re.search(r"designation\s+(?:for\s+)?[\"']?([A-Za-z][A-Za-z0-9 &\-/]{3,60})", q, re.I)
    name = m.group(1).strip() if m else None
    if not name:
        return None
    rows = hrms_execute(
        """
        SELECT TOP 5 ID, DesignationName, SanctionedStrength
        FROM dbo.pr_designation
        WHERE LOWER(DesignationName) LIKE :n
        ORDER BY LEN(DesignationName)
        """,
        {"n": f"%{name.lower()}%"},
    )
    return rows[0] if rows else None


def enrich_officer(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize an employee row into rule-engine shape."""
    out = dict(row)
    gid = row.get("EmployeeGradeID")
    grade_code = row.get("GradeCode")
    if not grade_code and gid is not None:
        g = resolve_grade_id(grade_id=int(gid))
        if g:
            grade_code = g.get("GradeCode") or g.get("GradeName")
            out["GradeCode"] = grade_code
            out["GradeName"] = g.get("GradeName")
    bps = parse_bps(grade_code) or parse_bps(row.get("GradeName"))
    out["bps"] = bps
    if row.get("DesignationName") and not out.get("designation_title"):
        out["designation_title"] = row.get("DesignationName")
    out["service"] = infer_service(out) or infer_service(row) or row.get("service")
    out["is_excadre"] = infer_is_excadre(out) or infer_is_excadre(row)
    out["service_source"] = "hrms_fields" if out.get("service") else "unresolved"
    out["name"] = (
        row.get("name")
        or f"{(row.get('FirstName') or '').strip()} {(row.get('LastName') or '').strip()}".strip()
    )
    out["employee_id"] = row.get("ID") or row.get("EmployeeID")
    if row.get("DesignationID") is not None:
        out["DesignationID"] = row.get("DesignationID")
    return out


def fetch_officer_live(
    *,
    employee_id: int | None = None,
    cnic: str | None = None,
    name: str | None = None,
    scope: AccessScope | None = None,
) -> dict[str, Any] | None:
    ref = resolve_officer(employee_id=employee_id, cnic=cnic, name=name)
    row = ref.hrms_row
    if not row and ref.hrms_employee_id:
        rows = hrms_execute(
            """
            SELECT TOP 1 e.ID, e.FirstName, e.LastName, e.NICNo, e.EmployeeGradeID, e.StatusID,
                   e.LocationID, e.WorkingLocationID, e.OrgLevel2, e.OrgLevel3, e.OrgLevel4,
                   e.RetirementDate, e.DOB, e.IsAttachmentBasis, e.EmployeeCode, e.PositionID,
                   e.DesignationID, e.EmployeeGroupID,
                   g.GradeCode, g.GradeName, d.DesignationName, eg.EmployeeGroupName
            FROM dbo.pr_employee_mf e
            LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = e.EmployeeGradeID
            LEFT JOIN dbo.pr_designation d ON d.ID = e.DesignationID
            LEFT JOIN dbo.pr_employee_group_mf eg ON eg.ID = e.EmployeeGroupID
            WHERE e.ID = :id
            """,
            {"id": ref.hrms_employee_id},
        )
        row = rows[0] if rows else None
    if not row:
        return None
    # Prefer joined grade + designation fields when available
    try:
        if employee_id or ref.hrms_employee_id:
            eid = int(employee_id or ref.hrms_employee_id)  # type: ignore[arg-type]
            joined = hrms_execute(
                """
                SELECT TOP 1 e.ID, e.FirstName, e.LastName, e.NICNo, e.EmployeeGradeID, e.StatusID,
                       e.LocationID, e.WorkingLocationID, e.OrgLevel2, e.OrgLevel3, e.OrgLevel4,
                       e.RetirementDate, e.DOB, e.IsAttachmentBasis, e.EmployeeCode, e.PositionID,
                       e.DesignationID, e.EmployeeGroupID,
                       g.GradeCode, g.GradeName, d.DesignationName, eg.EmployeeGroupName
                FROM dbo.pr_employee_mf e
                LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = e.EmployeeGradeID
                LEFT JOIN dbo.pr_designation d ON d.ID = e.DesignationID
                LEFT JOIN dbo.pr_employee_group_mf eg ON eg.ID = e.EmployeeGroupID
                WHERE e.ID = :id
                """,
                {"id": eid},
            )
            if joined:
                row = joined[0]
    except Exception as e:
        log.debug("officer join enrich skipped: %s", e)

    officer = enrich_officer(row)
    officer["cnic"] = ref.cnic or officer.get("NICNo")
    officer["cnics"] = ref.cnics
    officer["pf_exists"] = ref.pf_exists
    if scope and not employee_in_scope(scope, officer):
        return None
    return officer


def discipline_flags_from_pf(cnics: list[str] | None) -> dict[str, bool]:
    """Best-effort open E&D / inquiry signals from OCR timeline."""
    flags = {"open_ed": False, "open_inquiry": False, "covering_suspension": False}
    if not cnics:
        return flags
    try:
        variants = []
        for c in cnics:
            variants.extend(cnic_variants(c))
        variants = list(dict.fromkeys(variants))
        if not variants:
            return flags
        params = {f"c{i}": v for i, v in enumerate(variants[:20])}
        in_list = ", ".join(f":c{i}" for i in range(len(params)))
        digs = list({cnic_digits(c) for c in variants if cnic_digits(c)})[:10]
        dig_parts = []
        for i, d in enumerate(digs):
            params[f"d{i}"] = d
            dig_parts.append(
                f"REPLACE(REPLACE(ISNULL(employee_cnic,''),'-',''),' ','') = :d{i}"
            )
        where = f"employee_cnic IN ({in_list})"
        if dig_parts:
            where = f"({where}) OR ({' OR '.join(dig_parts)})"
        rows = ocr_execute(
            f"""
            SELECT TOP 30 event_type, event_subtype, main_category, outcome, summary, charges_summary
            FROM dbo.hr_employee_event_timeline_dt
            WHERE ({where})
              AND (
                LOWER(ISNULL(main_category,'')) LIKE '%inquir%'
                OR LOWER(ISNULL(main_category,'')) LIKE '%discipline%'
                OR LOWER(ISNULL(main_category,'')) LIKE '%complaint%'
                OR LOWER(ISNULL(event_type,'')) LIKE '%e&d%'
                OR LOWER(ISNULL(event_type,'')) LIKE '%inquir%'
                OR LOWER(ISNULL(event_subtype,'')) LIKE '%suspension%'
              )
            ORDER BY COALESCE(event_date, document_date) DESC
            """,
            params,
        )
        for r in rows:
            blob = " ".join(
                str(r.get(k) or "") for k in ("event_type", "event_subtype", "main_category", "outcome", "summary")
            ).lower()
            closed = any(x in blob for x in ("closed", "exonerat", "acquit", "dropped", "withdrawn"))
            if closed:
                continue
            if "suspension" in blob:
                flags["covering_suspension"] = True
            if "e&d" in blob or "enquiry" in blob or "inquiry" in blob or "discipline" in blob:
                flags["open_ed"] = True
                flags["open_inquiry"] = True
            if "complaint" in blob:
                flags["open_inquiry"] = True
    except Exception as e:
        log.debug("PF discipline flags skipped: %s", e)
    return flags


def fetch_seat_live(
    *,
    position_id: int | None = None,
    grade_id: int | None = None,
    designation_id: int | None = None,
    location_id: int | None = None,
    question: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a concrete position/seat from HRMS for succession/transfer."""
    if position_id is not None:
        rows = hrms_execute(
            """
            SELECT TOP 1 p.ID, p.PositionName, p.GradeID, p.DesignationID, p.LocationID, p.OrgLevel2,
                   g.GradeCode, g.GradeName, d.DesignationName
            FROM dbo.pr_position_mf p
            LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = p.GradeID
            LEFT JOIN dbo.pr_designation d ON d.ID = p.DesignationID
            WHERE p.ID = :pid
            """,
            {"pid": position_id},
        )
        if rows:
            return _seat_from_row(rows[0])

    gid = grade_id
    if gid is None and question:
        g = resolve_grade_id(question=question)
        if g:
            gid = int(g["ID"])

    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if gid is not None:
        clauses.append("p.GradeID = :gid")
        params["gid"] = gid
    if designation_id is not None:
        clauses.append("p.DesignationID = :did")
        params["did"] = designation_id
    if location_id is not None:
        clauses.append("p.LocationID = :lid")
        params["lid"] = location_id

    # Prefer vacant chairs: positions with no holding occupant
    holding = load_status_map().get("holding_occupy_ids") or get_settings().holding_status_ids
    hold_csv = ", ".join(str(int(x)) for x in holding) or "-1"
    where = " AND ".join(clauses)
    try:
        rows = hrms_execute(
            f"""
            SELECT TOP 25
              p.ID, p.PositionName, p.GradeID, p.DesignationID, p.LocationID, p.OrgLevel2,
              g.GradeCode, g.GradeName, d.DesignationName,
              (
                SELECT COUNT(*) FROM dbo.pr_employee_mf e
                WHERE e.PositionID = p.ID AND e.StatusID IN ({hold_csv})
              ) AS Occupants
            FROM dbo.pr_position_mf p
            LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = p.GradeID
            LEFT JOIN dbo.pr_designation d ON d.ID = p.DesignationID
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
    except Exception as e:
        log.warning("vacant-prefer seat query failed, simple fallback: %s", e)
        rows = hrms_execute(
            f"""
            SELECT TOP 25
              p.ID, p.PositionName, p.GradeID, p.DesignationID, p.LocationID, p.OrgLevel2,
              g.GradeCode, g.GradeName, d.DesignationName, 0 AS Occupants
            FROM dbo.pr_position_mf p
            LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = p.GradeID
            LEFT JOIN dbo.pr_designation d ON d.ID = p.DesignationID
            WHERE {where}
            ORDER BY p.ID
            """,
            params,
        )
    if not rows:
        return None
    # Prefer first vacant
    vacant = [r for r in rows if int(r.get("Occupants") or 0) == 0]
    chosen = vacant[0] if vacant else rows[0]
    return _seat_from_row(chosen)


def _seat_from_row(r: dict[str, Any]) -> dict[str, Any]:
    bps = parse_bps(r.get("GradeCode")) or parse_bps(r.get("GradeName"))
    return {
        "ID": r.get("ID"),
        "position_id": r.get("ID"),
        "PositionName": r.get("PositionName"),
        "GradeID": r.get("GradeID"),
        "grade_id": r.get("GradeID"),
        "DesignationID": r.get("DesignationID"),
        "LocationID": r.get("LocationID"),
        "OrgLevel2": r.get("OrgLevel2"),
        "GradeCode": r.get("GradeCode"),
        "GradeName": r.get("GradeName"),
        "designation_title": r.get("DesignationName") or r.get("PositionName"),
        "bps": bps,
        "service": infer_service(r),
        "is_excadre": infer_is_excadre(r),
        "occupants": int(r.get("Occupants") or 0),
        "vacant": int(r.get("Occupants") or 0) == 0,
    }


def fetch_candidates_live(
    seat: dict[str, Any],
    *,
    scope: AccessScope,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Live candidate pool for succession — same/adjacent BPS, in AccessScope."""
    holding = load_status_map().get("holding_occupy_ids") or get_settings().holding_status_ids
    hold_csv = ", ".join(str(int(x)) for x in holding) or "-1"
    seat_bps = parse_bps(seat.get("bps") or seat.get("GradeCode"))
    seat_gid = seat.get("GradeID") or seat.get("grade_id")

    clauses = [f"e.StatusID IN ({hold_csv})"]
    params: dict[str, Any] = {}
    top_n = max(1, min(int(limit) * 3, 200))  # over-fetch then filter by BPS/scope

    # Scope predicates
    if scope.level is not None:
        from app.auth.rbac import AccessID

        if scope.level == AccessID.SELF:
            if scope.employee_id is None:
                return []
            clauses.append("e.ID = :self_id")
            params["self_id"] = scope.employee_id
        elif scope.level == AccessID.LOCATION and scope.location_ids:
            ids = ", ".join(str(int(x)) for x in scope.location_ids)
            clauses.append(f"(e.LocationID IN ({ids}) OR e.WorkingLocationID IN ({ids}))")
        elif scope.level == AccessID.ORG and scope.org_structure_ids:
            org_ids = ", ".join(str(int(x)) for x in scope.org_structure_ids)
            org_cols = " OR ".join(f"e.OrgLevel{i} IN ({org_ids})" for i in range(2, 10))
            clauses.append(f"({org_cols})")

    # BPS proximity filtered in Python after enrich; avoid brittle GradeCode SQL
    _ = seat_gid  # reserved for future grade-family filter

    where = " AND ".join(clauses)
    try:
        rows = hrms_execute(
            f"""
            SELECT TOP {top_n}
              e.ID, e.FirstName, e.LastName, e.NICNo, e.EmployeeGradeID, e.StatusID,
              e.LocationID, e.WorkingLocationID, e.OrgLevel2, e.OrgLevel3,
              e.RetirementDate, e.DOB, e.PositionID, e.IsAttachmentBasis,
              g.GradeCode, g.GradeName
            FROM dbo.pr_employee_mf e
            LEFT JOIN dbo.pr_employee_grades_mf g ON g.ID = e.EmployeeGradeID
            WHERE {where}
            ORDER BY e.EmployeeGradeID DESC, e.ID
            """,
            params,
        )
    except Exception as e:
        log.warning("candidate query failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        cand = enrich_officer(r)
        if seat_bps is not None and cand.get("bps") is not None:
            # Keep same grade or one below for OPS; drop far grades
            if abs(int(cand["bps"]) - int(seat_bps)) > 1:
                continue
        if not employee_in_scope(scope, cand):
            continue
        # Discipline from PF when CNIC present
        cnics = cnic_variants(str(cand.get("NICNo") or ""))
        flags = discipline_flags_from_pf(cnics)
        cand.update(flags)
        cand["same_chair"] = bool(
            cand.get("PositionID") and seat.get("ID") and int(cand["PositionID"]) == int(seat["ID"])
        )
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def completed_trainings_for(employee_id: int) -> list[str]:
    try:
        rows = hrms_execute(
            """
            SELECT TOP 50 t.CourseName, t.CourseCode, t.TrainingName
            FROM dbo.hr_employee_training_dt t
            WHERE t.EmployeeID = :eid
            """,
            {"eid": employee_id},
        )
    except Exception:
        try:
            rows = hrms_execute(
                """
                SELECT TOP 50 CourseName
                FROM dbo.hr_training_nomination_dt
                WHERE EmployeeID = :eid AND (Status LIKE '%complet%' OR IsCompleted = 1)
                """,
                {"eid": employee_id},
            )
        except Exception as e:
            log.debug("training history unavailable: %s", e)
            return []
    names: list[str] = []
    for r in rows:
        for k in ("CourseName", "CourseCode", "TrainingName"):
            if r.get(k):
                names.append(str(r[k]).upper())
    # Normalize common codes
    normed = []
    for n in names:
        for code in ("MCMC", "SMC", "NMC", "NSWC", "NDC"):
            if code in n:
                normed.append(code)
        normed.append(n)
    return list(dict.fromkeys(normed))


def schedule_ii_years(from_bps: int, to_bps: int) -> float | None:
    """Live Schedule-II from policy_facts.db (no hardcoded bars)."""
    try:
        from app.sources.policies.retrieve import schedule_ii_years as _pol_years

        return _pol_years(from_bps, to_bps)
    except Exception as e:
        log.debug("schedule_ii lookup failed: %s", e)
        return None


def schedule_ii_lookup(from_bps: int, to_bps: int) -> dict[str, Any]:
    """Return years + provenance; MISSING when policy facts lack the hop."""
    try:
        from app.sources.policies.retrieve import schedule_ii_entry

        entry = schedule_ii_entry(from_bps, to_bps)
        if entry:
            return {
                "min_years": float(entry["min_years"]),
                "note": entry.get("note"),
                "source": "policy_facts.schedule_ii",
                "status": "ok",
            }
    except Exception as e:
        log.debug("schedule_ii_lookup failed: %s", e)
    return {
        "min_years": None,
        "source": None,
        "status": "MISSING",
        "note": f"No Schedule-II row for BPS {from_bps}→{to_bps} in policy_facts — re-run ingest",
    }


def qualifying_service_years(employee_id: int) -> float | None:
    """Approximate BS-17+ cumulative service minus EOL from HRMS when tables exist."""
    try:
        rows = hrms_execute(
            """
            SELECT TOP 1
              DATEDIFF(day, COALESCE(DateOfAppointment, DateOfJoining, DOJ), GETDATE()) / 365.25 AS years
            FROM dbo.pr_employee_mf
            WHERE ID = :eid
            """,
            {"eid": employee_id},
        )
        if rows and rows[0].get("years") is not None:
            years = float(rows[0]["years"])
            # Subtract EOL if available
            try:
                eol = hrms_execute(
                    """
                    SELECT TOP 1 ISNULL(SUM(DATEDIFF(day, FromDate, ToDate)), 0) / 365.25 AS eol_years
                    FROM dbo.hr_employee_leave_dt
                    WHERE EmployeeID = :eid AND (
                      LOWER(LeaveType) LIKE '%eol%'
                      OR LOWER(LeaveType) LIKE '%extra ordinary%'
                      OR LOWER(LeaveType) LIKE '%extraordinary%'
                    )
                    """,
                    {"eid": employee_id},
                )
                if eol and eol[0].get("eol_years") is not None:
                    years -= float(eol[0]["eol_years"])
            except Exception:
                pass
            return max(0.0, round(years, 2))
    except Exception as e:
        log.debug("qualifying service years unavailable: %s", e)
    return None


def fetch_course_live(question: str | None = None, course_name: str | None = None) -> dict[str, Any] | None:
    name = course_name
    if not name and question:
        for code in ("MCMC", "SMC", "NMC", "NSWC", "NDC"):
            if re.search(rf"\b{code}\b", question, re.I):
                name = code
                break
        if not name:
            m = re.search(r"(?:training|course|nomination)\s+(?:for\s+)?[\"']?([A-Za-z0-9 \-]{2,40})", question or "", re.I)
            if m:
                name = m.group(1).strip()
    if not name:
        return None
    try:
        rows = hrms_execute(
            """
            SELECT TOP 5 ID, CourseName, CourseCode, AgeMin, AgeMax,
                   EligibilityOrgLevel2Ids
            FROM dbo.hr_training_course_mf
            WHERE LOWER(CourseName) LIKE :n OR LOWER(ISNULL(CourseCode,'')) LIKE :n
            ORDER BY ID DESC
            """,
            {"n": f"%{name.lower()}%"},
        )
        if rows:
            r = rows[0]
            return {
                "ID": r.get("ID"),
                "CourseID": r.get("ID"),
                "CourseName": r.get("CourseName") or name,
                "age_min": r.get("AgeMin"),
                "age_max": r.get("AgeMax"),
                "EligibilityOrgLevel2Ids": r.get("EligibilityOrgLevel2Ids"),
            }
    except Exception as e:
        log.debug("course mf lookup failed: %s", e)
    # Fallback: named course without HRMS row (still evaluate age/LMS rules)
    return {"CourseName": name.upper() if len(name) <= 8 else name}


def extract_excadre_hops_from_policies() -> list[dict[str, str]]:
    """Best-effort hop extraction from policy_facts chunks mentioning Ex-Cadre designations."""
    path = get_settings().policy_facts_db
    if not path.exists():
        return []
    hops: list[dict[str, str]] = []
    try:
        conn = sqlite3.connect(str(path))
        rows = conn.execute(
            """
            SELECT text FROM chunks
            WHERE LOWER(text) LIKE '%ex-cadre%' OR LOWER(text) LIKE '%excadre%'
               OR LOWER(doc_type) LIKE '%promotion%'
            LIMIT 400
            """
        ).fetchall()
        conn.close()
        pat = re.compile(
            r"([A-Z][A-Za-z /&\-]{4,40}?)\s+(?:to|→|->|promoted to)\s+([A-Z][A-Za-z /&\-]{4,40})",
            re.M,
        )
        for (text,) in rows:
            for m in pat.finditer(text or ""):
                a, b = m.group(1).strip(" .;,"), m.group(2).strip(" .;,")
                if len(a) < 5 or len(b) < 5:
                    continue
                if a.lower() == b.lower():
                    continue
                hops.append({"from": a, "to": b, "gazette": "policy_extract"})
        # de-dupe
        seen = set()
        uniq = []
        for h in hops:
            key = (h["from"].lower(), h["to"].lower())
            if key in seen:
                continue
            seen.add(key)
            uniq.append(h)
        return uniq[:200]
    except Exception as e:
        log.debug("excadre hop extract failed: %s", e)
        return []


def cnic_coverage_report() -> dict[str, Any]:
    """Report HRMS↔OCR CNIC bridge coverage for ops dashboards."""
    try:
        hrms_n = hrms_execute(
            """
            SELECT COUNT(*) AS n FROM dbo.pr_employee_mf
            WHERE NICNo IS NOT NULL AND LEN(REPLACE(REPLACE(NICNo,'-',''),' ','')) >= 11
            """
        )[0]["n"]
    except Exception as e:
        return {"ok": False, "error": f"hrms: {e}"}
    try:
        ocr_n = ocr_execute(
            """
            SELECT COUNT(DISTINCT REPLACE(REPLACE(ISNULL(employee_cnic,''),'-',''),' ','')) AS n
            FROM dbo.hr_file_360_mf
            WHERE LEN(REPLACE(REPLACE(ISNULL(employee_cnic,''),'-',''),' ','')) >= 11
            """
        )[0]["n"]
    except Exception as e:
        return {"ok": False, "error": f"ocr: {e}", "hrms_with_cnic": hrms_n}
    return {
        "ok": True,
        "hrms_with_cnic": int(hrms_n),
        "ocr_distinct_cnic": int(ocr_n),
        "note": "Overlap requires join on digit-normalized CNIC; use scripts/cnic_coverage.py for full overlap.",
    }


def holding_status_ids() -> list[int]:
    m = load_status_map()
    ids = m.get("holding_occupy_ids") or get_settings().holding_status_ids
    return [int(x) for x in ids]
