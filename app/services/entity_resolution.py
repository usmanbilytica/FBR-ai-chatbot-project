from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.db.hrms import hrms_execute
from app.db.ocr import ocr_execute


def cnic_digits(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def cnic_variants(value: str | None) -> list[str]:
    digits = cnic_digits(value)
    if not digits:
        return []
    variants = {digits}
    if len(digits) == 13:
        variants.add(f"{digits[:5]}-{digits[5:12]}-{digits[12]}")
    return list(variants)


def usable_cnic(value: str | None) -> bool:
    return len(cnic_digits(value)) >= 11


@dataclass
class OfficerRef:
    hrms_employee_id: int | None = None
    cnic: str | None = None
    name: str | None = None
    cnics: list[str] = field(default_factory=list)
    pf_exists: bool = False
    hrms_row: dict[str, Any] | None = None


def resolve_officer(
    *,
    employee_id: int | None = None,
    cnic: str | None = None,
    name: str | None = None,
) -> OfficerRef:
    ref = OfficerRef(hrms_employee_id=employee_id, cnic=cnic, name=name)
    if employee_id is not None:
        rows = hrms_execute(
            """
            SELECT TOP 1 ID, FirstName, LastName, NICNo, EmployeeGradeID, StatusID,
                   LocationID, WorkingLocationID, OrgLevel2, RetirementDate, DOB,
                   IsAttachmentBasis, EmployeeCode
            FROM dbo.pr_employee_mf WHERE ID = :id
            """,
            {"id": employee_id},
        )
        if rows:
            ref.hrms_row = rows[0]
            ref.cnic = str(rows[0].get("NICNo") or "") or None
            fn = (rows[0].get("FirstName") or "").strip()
            ln = (rows[0].get("LastName") or "").strip()
            ref.name = f"{fn} {ln}".strip() or name

    if ref.cnic and usable_cnic(ref.cnic):
        ref.cnics = cnic_variants(ref.cnic)
    elif name:
        # HRMS name lookup
        rows = hrms_execute(
            """
            SELECT TOP 5 ID, FirstName, LastName, NICNo
            FROM dbo.pr_employee_mf
            WHERE LOWER(LTRIM(RTRIM(FirstName)) + ' ' + LTRIM(RTRIM(ISNULL(LastName,''))))
                  LIKE :q
            """,
            {"q": f"%{name.strip().lower()}%"},
        )
        if rows:
            ref.hrms_row = rows[0]
            ref.hrms_employee_id = int(rows[0]["ID"])
            ref.cnic = str(rows[0].get("NICNo") or "") or None
            ref.cnics = cnic_variants(ref.cnic)
            fn = (rows[0].get("FirstName") or "").strip()
            ln = (rows[0].get("LastName") or "").strip()
            ref.name = f"{fn} {ln}".strip()

    # Expand CNICs from OCR by name if needed
    if not ref.cnics and name:
        orows = ocr_execute(
            """
            SELECT DISTINCT TOP 10 employee_cnic, employee_name
            FROM dbo.hr_file_360_mf
            WHERE LOWER(LTRIM(RTRIM(employee_name))) LIKE :q
            """,
            {"q": f"%{name.strip().lower()}%"},
        )
        for r in orows:
            ref.cnics.extend(cnic_variants(r.get("employee_cnic")))
        ref.cnics = list(dict.fromkeys(ref.cnics))

    if ref.cnics:
        placeholders = ", ".join(f":c{i}" for i in range(len(ref.cnics)))
        params = {f"c{i}": v for i, v in enumerate(ref.cnics)}
        # Also match digit-normalized
        digs = list({cnic_digits(c) for c in ref.cnics if cnic_digits(c)})
        dig_clause = " OR ".join(
            f"REPLACE(REPLACE(ISNULL(employee_cnic,''),'-',''),' ','') = :d{i}"
            for i in range(len(digs))
        )
        for i, d in enumerate(digs):
            params[f"d{i}"] = d
        where = f"employee_cnic IN ({placeholders})"
        if dig_clause:
            where = f"({where}) OR ({dig_clause})"
        hit = ocr_execute(
            f"SELECT TOP 1 id FROM dbo.hr_file_360_mf WHERE {where}",
            params,
        )
        ref.pf_exists = bool(hit)
        if not hit:
            hit = ocr_execute(
                f"SELECT TOP 1 id FROM dbo.hr_page_360_dt WHERE {where}",
                params,
            )
            ref.pf_exists = bool(hit)

    return ref
