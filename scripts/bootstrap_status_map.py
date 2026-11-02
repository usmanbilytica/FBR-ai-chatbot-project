#!/usr/bin/env python
"""Bootstrap status_map.json from live HRMS dropdown values."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.db.hrms import hrms_execute
from app.rules.status_map import DEFAULT_STATUS_MAP, write_status_map


KEYWORDS = {
    "suspension": ["suspend"],
    "deputation": ["deput"],
    "training": ["training"],
    "study_leave": ["study leave", "study-leave"],
    "lpr": ["lpr", "leave preparatory"],
    "awaiting_posting": ["awaiting posting", "awaiting-posting", "general"],
    "resignation_in_process": ["resign"],
    "serving": ["serving", "active"],
}


def main() -> None:
    settings = get_settings()
    # Discover dropdown value columns dynamically
    cols = hrms_execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME='sys_drop_down_value'
        """
    )
    colnames = {c["COLUMN_NAME"] for c in cols}
    label_col = "Name" if "Name" in colnames else (
        "Value" if "Value" in colnames else ("DropDownValue" if "DropDownValue" in colnames else None)
    )
    if not label_col:
        # fallback probe
        for cand in ("DisplayName", "Title", "Text", "Description"):
            if cand in colnames:
                label_col = cand
                break
    data = json.loads(json.dumps(DEFAULT_STATUS_MAP))
    if not label_col:
        print("Could not find label column on sys_drop_down_value; writing defaults")
        write_status_map(data)
        return

    rows = hrms_execute(
        f"""
        SELECT ID, [{label_col}] AS Label
        FROM dbo.sys_drop_down_value
        """
    )
    labels_by_id = {}
    by_label = {k: [] for k in KEYWORDS}
    for r in rows:
        lid = int(r["ID"])
        lab = str(r.get("Label") or "").strip()
        labels_by_id[str(lid)] = lab
        low = lab.lower()
        for key, kws in KEYWORDS.items():
            if any(k in low for k in kws):
                by_label[key].append(lid)
    data["by_label"] = by_label
    data["labels_by_id"] = labels_by_id
    data["holding_occupy_ids"] = settings.holding_status_ids
    path = write_status_map(data)
    print(f"Wrote {path}")
    for k, v in by_label.items():
        print(f"  {k}: {v[:12]}{'...' if len(v)>12 else ''}")


if __name__ == "__main__":
    main()
