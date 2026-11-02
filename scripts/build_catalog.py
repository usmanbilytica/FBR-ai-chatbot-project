#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.sources.hrms.catalog import build_live_catalog


def main() -> None:
    cat = build_live_catalog()
    print(f"Catalog tables: {cat['table_count']}")


if __name__ == "__main__":
    main()
