from __future__ import annotations

from typing import Any, Literal

from app.core.config import get_settings
from app.db.hrms import hrms_execute
from app.rules.types import RuleResult, Severity

Lens = Literal["grade", "designation", "position"]


def _holding_ids() -> list[int]:
    """Prefer live status_map holding_occupy_ids; fall back to settings."""
    try:
        from app.rules.status_map import load_status_map

        ids = load_status_map().get("holding_occupy_ids") or []
        if ids:
            return [int(x) for x in ids]
    except Exception:
        pass
    return list(get_settings().holding_status_ids)


def _occupied_employees(
    *,
    grade_id: int | None = None,
    designation_id: int | None = None,
    location_id: int | None = None,
    org_level2: int | None = None,
) -> list[dict[str, Any]]:
    ids = _holding_ids()
    placeholders = ", ".join(str(int(i)) for i in ids) or "-1"
    clauses = [f"e.StatusID IN ({placeholders})"]
    params: dict[str, Any] = {}
    if grade_id is not None:
        clauses.append("e.EmployeeGradeID = :gid")
        params["gid"] = grade_id
    if designation_id is not None:
        # designation often via action/current position — approximate via Position join when present
        clauses.append(
            """EXISTS (
                SELECT 1 FROM dbo.pr_position_mf p
                WHERE p.ID = e.PositionID AND p.DesignationID = :did
            )"""
        )
        params["did"] = designation_id
    if location_id is not None:
        clauses.append("(e.LocationID = :lid OR e.WorkingLocationID = :lid)")
        params["lid"] = location_id
    if org_level2 is not None:
        clauses.append("e.OrgLevel2 = :o2")
        params["o2"] = org_level2
    where = " AND ".join(clauses)
    # PositionID may not exist on all schemas — fallback without designation filter
    try:
        return hrms_execute(
            f"""
            SELECT e.ID, e.FirstName, e.LastName, e.EmployeeGradeID, e.StatusID,
                   e.LocationID, e.WorkingLocationID, e.OrgLevel2
            FROM dbo.pr_employee_mf e
            WHERE {where}
            """,
            params,
        )
    except Exception:
        if designation_id is not None:
            params.pop("did", None)
            clauses = [c for c in clauses if "DesignationID" not in c]
            where = " AND ".join(clauses)
            return hrms_execute(
                f"""
                SELECT e.ID, e.FirstName, e.LastName, e.EmployeeGradeID, e.StatusID,
                       e.LocationID, e.WorkingLocationID, e.OrgLevel2
                FROM dbo.pr_employee_mf e
                WHERE {where}
                """,
                params,
            )
        raise


def sanction_strength(
    lens: Lens,
    *,
    grade_id: int | None = None,
    designation_id: int | None = None,
    location_id: int | None = None,
    org_level2: int | None = None,
) -> RuleResult:
    sanctioned = 0.0
    evidence: list[dict[str, Any]] = []

    if lens == "grade":
        if grade_id is None:
            return RuleResult(Severity.MISSING, reasons=["grade_id required for grade lens"])
        if org_level2 is not None:
            rows = hrms_execute(
                """
                SELECT SanctionedBudget AS SanctionedStrength, GradeID, OrgLevel2
                FROM dbo.pr_employee_grade_budget_allocation
                WHERE GradeID = :gid AND OrgLevel2 = :o2
                """,
                {"gid": grade_id, "o2": org_level2},
            )
            if rows:
                sanctioned = float(rows[0].get("SanctionedStrength") or 0)
                evidence.append({"source": "pr_employee_grade_budget_allocation", **rows[0]})
        if not evidence:
            rows = hrms_execute(
                """
                SELECT ID, GradeName, GradeCode, SanctionedStrength
                FROM dbo.pr_employee_grades_mf WHERE ID = :gid
                """,
                {"gid": grade_id},
            )
            if not rows:
                return RuleResult(Severity.MISSING, reasons=[f"grade_id {grade_id} not found"])
            sanctioned = float(rows[0].get("SanctionedStrength") or 0)
            evidence.append({"source": "pr_employee_grades_mf", **rows[0]})

    elif lens == "designation":
        if designation_id is None:
            return RuleResult(Severity.MISSING, reasons=["designation_id required for designation lens"])
        clauses = ["DesignationID = :did"]
        params: dict[str, Any] = {"did": designation_id}
        if org_level2 is not None:
            clauses.append("OrgLevel2 = :o2")
            params["o2"] = org_level2
        if location_id is not None:
            clauses.append("LocationID = :lid")
            params["lid"] = location_id
        where = " AND ".join(clauses)
        rows = hrms_execute(
            f"""
            SELECT DesignationID, OrgLevel2, LocationID, SanctionedStrength
            FROM dbo.pr_designation_sanctioned_allocation
            WHERE {where}
            """,
            params,
        )
        if rows:
            sanctioned = float(sum(float(r.get("SanctionedStrength") or 0) for r in rows))
            evidence.extend({"source": "pr_designation_sanctioned_allocation", **r} for r in rows[:20])
        else:
            rows = hrms_execute(
                "SELECT ID, DesignationName, SanctionedStrength FROM dbo.pr_designation WHERE ID = :did",
                {"did": designation_id},
            )
            if rows:
                sanctioned = float(rows[0].get("SanctionedStrength") or 0)
                evidence.append({"source": "pr_designation", **rows[0]})

    elif lens == "position":
        clauses = ["1=1"]
        params = {}
        if grade_id is not None:
            clauses.append("GradeID = :gid")
            params["gid"] = grade_id
        if designation_id is not None:
            clauses.append("DesignationID = :did")
            params["did"] = designation_id
        if location_id is not None:
            clauses.append("LocationID = :lid")
            params["lid"] = location_id
        if org_level2 is not None:
            clauses.append("OrgLevel2 = :o2")
            params["o2"] = org_level2
        where = " AND ".join(clauses)
        rows = hrms_execute(
            f"SELECT COUNT(*) AS n FROM dbo.pr_position_mf WHERE {where}",
            params,
        )
        sanctioned = float(rows[0]["n"] if rows else 0)
        evidence.append({"source": "pr_position_mf", "count": sanctioned, "filters": params})
    else:
        return RuleResult(Severity.FAIL, reasons=[f"unknown lens {lens}"])

    occupied_rows = _occupied_employees(
        grade_id=grade_id if lens in ("grade", "position") else None,
        designation_id=designation_id if lens == "designation" else None,
        location_id=location_id,
        org_level2=org_level2,
    )
    occupied = len(occupied_rows)
    vacant = sanctioned - occupied

    return RuleResult(
        status=Severity.PASS,
        reasons=[f"{lens} sanctioned={sanctioned} occupied={occupied} vacant={vacant}"],
        evidence=evidence,
        citations=[f"lens:{lens}"],
        data={
            "lens": lens,
            "sanctioned": sanctioned,
            "occupied": occupied,
            "vacant": vacant,
            "occupied_sample_ids": [r.get("ID") for r in occupied_rows[:25]],
        },
    )
