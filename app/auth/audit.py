from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from app.core.config import get_settings


def _conn() -> sqlite3.Connection:
    path = get_settings().audit_db
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            user_id INTEGER,
            access_id INTEGER,
            question TEXT,
            topic TEXT,
            answer TEXT,
            sql_json TEXT,
            pf_json TEXT,
            policy_json TEXT,
            rule_json TEXT,
            duration_ms REAL,
            flagged INTEGER DEFAULT 0
        )
        """
    )
    c.commit()
    return c


def new_request_id() -> str:
    return uuid.uuid4().hex


def write_audit(
    *,
    request_id: str,
    user_id: int | None,
    access_id: int | None,
    question: str,
    topic: str,
    answer: str,
    sql_events: list[Any] | None = None,
    pf_events: list[Any] | None = None,
    policy_events: list[Any] | None = None,
    rule_events: list[Any] | None = None,
    duration_ms: float = 0.0,
) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO audit_log (
              id, ts, user_id, access_id, question, topic, answer,
              sql_json, pf_json, policy_json, rule_json, duration_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id,
                time.time(),
                user_id,
                access_id,
                question,
                topic,
                answer,
                json.dumps(sql_events or [], default=str),
                json.dumps(pf_events or [], default=str),
                json.dumps(policy_events or [], default=str),
                json.dumps(rule_events or [], default=str),
                duration_ms,
            ),
        )
        c.commit()


def flag_request(request_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE audit_log SET flagged = 1 WHERE id = ?", (request_id,))
        c.commit()
        return cur.rowcount > 0
