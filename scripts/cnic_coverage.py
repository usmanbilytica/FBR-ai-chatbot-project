#!/usr/bin/env python
"""Report HRMS ↔ OCR CNIC bridge coverage (digit-normalized overlap sample)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.hrms import hrms_execute
from app.db.ocr import ocr_execute
from app.services.entity_resolution import cnic_digits
from app.sources.hrms.live_resolve import cnic_coverage_report


def main() -> None:
    report = cnic_coverage_report()
    print(report)
    if not report.get("ok"):
        sys.exit(1)

    # Sample overlap: take up to 5000 HRMS CNICs and check OCR presence
    hrms_rows = hrms_execute(
        """
        SELECT TOP 5000 NICNo FROM dbo.pr_employee_mf
        WHERE NICNo IS NOT NULL AND LEN(REPLACE(REPLACE(NICNo,'-',''),' ','')) >= 11
        ORDER BY ID
        """
    )
    digits = [cnic_digits(r.get("NICNo")) for r in hrms_rows]
    digits = [d for d in digits if len(d) >= 11]
    hit = 0
    checked = 0
    for d in digits[:2000]:
        checked += 1
        rows = ocr_execute(
            """
            SELECT TOP 1 id FROM dbo.hr_file_360_mf
            WHERE REPLACE(REPLACE(ISNULL(employee_cnic,''),'-',''),' ','') = :d
            """,
            {"d": d},
        )
        if rows:
            hit += 1
        if checked % 200 == 0:
            print(f"  checked {checked}/{min(2000, len(digits))} overlap={hit}")
    pct = (100.0 * hit / checked) if checked else 0.0
    print({"sample_checked": checked, "ocr_hit": hit, "overlap_pct_approx": round(pct, 2)})


if __name__ == "__main__":
    main()
