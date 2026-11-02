from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Result

from app.core.config import get_settings

log = logging.getLogger(__name__)


def _odbc_connect_string(server: str, database: str, user: str, password: str, driver: str) -> str:
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=20;"
    )


@lru_cache
def get_ocr_engine() -> Engine:
    s = get_settings()
    odbc = _odbc_connect_string(
        s.ocr_db_server, s.ocr_db_name, s.ocr_db_user, s.ocr_db_password, s.odbc_driver
    )
    url = f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc)}"
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def ocr_execute(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    eng = get_ocr_engine()
    with eng.connect() as conn:
        result: Result = conn.execute(text(sql), params or {})
        if result.returns_rows:
            cols = list(result.keys())
            return [dict(zip(cols, row)) for row in result.fetchall()]
        return []


def ocr_ping() -> bool:
    try:
        rows = ocr_execute("SELECT 1 AS ok")
        return bool(rows and rows[0].get("ok") == 1)
    except Exception as e:
        log.warning("OCR ping failed: %s", e)
        return False
