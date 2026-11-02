from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings

log = logging.getLogger(__name__)

DEFAULT_STATUS_MAP: dict[str, Any] = {
    "holding_occupy_ids": [1, 22, 23],
    "by_label": {
        "serving": [1],
        "suspension": [],
        "deputation": [],
        "training": [],
        "study_leave": [],
        "lpr": [],
        "awaiting_posting": [],
        "resignation_in_process": [],
    },
    "labels_by_id": {},
}


@lru_cache
def load_status_map() -> dict[str, Any]:
    path = get_settings().status_map_path
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed reading status_map: %s", e)
    return dict(DEFAULT_STATUS_MAP)


def status_ids_for(*labels: str) -> set[int]:
    m = load_status_map()
    out: set[int] = set()
    by = m.get("by_label") or {}
    for lab in labels:
        for i in by.get(lab, []) or []:
            out.add(int(i))
    return out


def is_blocked_status(status_id: int | None) -> tuple[bool, str]:
    if status_id is None:
        return False, ""
    sid = int(status_id)
    checks = [
        ("suspension", "Under Suspension"),
        ("deputation", "On Deputation"),
        ("training", "On Training"),
        ("study_leave", "Study Leave"),
        ("lpr", "LPR"),
    ]
    for key, label in checks:
        if sid in status_ids_for(key):
            return True, label
    return False, ""


def write_status_map(data: dict[str, Any], path: Path | None = None) -> Path:
    path = path or get_settings().status_map_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    load_status_map.cache_clear()
    return path
