from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from app.core.config import get_settings

log = logging.getLogger(__name__)


def _facts_conn() -> sqlite3.Connection:
    path = get_settings().policy_facts_db
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
          id INTEGER PRIMARY KEY,
          doc_name TEXT,
          section TEXT,
          text TEXT,
          doc_type TEXT,
          office_type TEXT,
          is_jd_book INTEGER DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_ii (
          id INTEGER PRIMARY KEY,
          from_bps INTEGER,
          to_bps INTEGER,
          min_years REAL,
          note TEXT
        )
        """
    )
    c.commit()
    return c


def search_policies(question: str, top_k: int = 6) -> list[dict[str, Any]]:
    """Policy retrieval: Qdrant `qdrant_policy` first, then SQLite keyword fallback.

    Legacy `data/qdrant` is no longer consulted (avoids stale partial indexes).
    """
    settings = get_settings()
    # 1) Vector search on dedicated policy store
    try:
        from qdrant_client import QdrantClient

        from app.services.embeddings import embed_query

        coll = settings.policy_collection
        store = settings.qdrant_policy_path
        client = QdrantClient(path=str(store))
        try:
            if client.collection_exists(coll):
                vec = embed_query(question)
                hits_raw: list[Any]
                if hasattr(client, "query_points"):
                    res = client.query_points(
                        collection_name=coll,
                        query=vec,
                        limit=top_k,
                        with_payload=True,
                    )
                    hits_raw = list(getattr(res, "points", None) or [])
                else:
                    hits_raw = client.search(
                        collection_name=coll,
                        query_vector=vec,
                        limit=top_k,
                    )
                out = []
                for h in hits_raw:
                    payload = getattr(h, "payload", None) or {}
                    out.append(
                        {
                            "score": getattr(h, "score", None),
                            "doc_name": payload.get("doc_name"),
                            "section": payload.get("section"),
                            "text": payload.get("text"),
                            "doc_type": payload.get("doc_type"),
                            "is_jd_book": payload.get("is_jd_book"),
                            "office_type": payload.get("office_type"),
                            "source": "qdrant_policy",
                        }
                    )
                if out:
                    return out
        finally:
            try:
                client.close()
            except Exception:
                pass
    except Exception as e:
        log.debug("Qdrant policy search unavailable: %s", e)

    # 2) SQLite keyword fallback (always available after ingest)
    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", question.lower()) if t not in {"the", "and", "for"}]
    with _facts_conn() as c:
        rows = c.execute(
            "SELECT id, doc_name, section, text, doc_type, office_type, is_jd_book FROM chunks"
        ).fetchall()
    scored = []
    for r in rows:
        text = (r["text"] or "").lower()
        score = sum(text.count(t) for t in tokens)
        if score:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, r in scored[:top_k]:
        r["source"] = "policy_facts_db"
        out.append(r)
    return out


def schedule_ii_years(from_bps: int, to_bps: int) -> float | None:
    """Prefer live schedule_ii table in policy_facts.db (populated by ingest)."""
    with _facts_conn() as c:
        row = c.execute(
            "SELECT min_years, note FROM schedule_ii WHERE from_bps=? AND to_bps=? ORDER BY id LIMIT 1",
            (from_bps, to_bps),
        ).fetchone()
    if row:
        return float(row["min_years"])
    return None


def schedule_ii_entry(from_bps: int, to_bps: int) -> dict[str, Any] | None:
    with _facts_conn() as c:
        row = c.execute(
            "SELECT from_bps, to_bps, min_years, note FROM schedule_ii WHERE from_bps=? AND to_bps=? ORDER BY id LIMIT 1",
            (from_bps, to_bps),
        ).fetchone()
    return dict(row) if row else None


def jd_authority() -> dict[str, Any]:
    path = get_settings().jd_authority_path
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    seed = get_settings().seed_dir / "jd_authority.json"
    if seed.exists():
        return json.loads(seed.read_text(encoding="utf-8"))
    return {"books": []}


def policy_corpus_stats() -> dict[str, Any]:
    stats: dict[str, Any] = {"chunks": 0, "schedule_ii_rows": 0, "qdrant_points": None}
    try:
        with _facts_conn() as c:
            stats["chunks"] = int(c.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
            stats["schedule_ii_rows"] = int(
                c.execute("SELECT COUNT(*) AS n FROM schedule_ii").fetchone()["n"]
            )
    except Exception as e:
        stats["db_error"] = str(e)
    try:
        from qdrant_client import QdrantClient

        s = get_settings()
        client = QdrantClient(path=str(s.qdrant_policy_path))
        try:
            if client.collection_exists(s.policy_collection):
                info = client.get_collection(s.policy_collection)
                stats["qdrant_points"] = int(getattr(info, "points_count", 0) or 0)
        finally:
            client.close()
    except Exception as e:
        stats["qdrant_error"] = str(e)
    return stats


def list_policy_docs(limit: int = 80) -> list[dict[str, Any]]:
    with _facts_conn() as c:
        rows = c.execute(
            """
            SELECT doc_name, doc_type, office_type, is_jd_book, COUNT(*) AS chunks
            FROM chunks
            GROUP BY doc_name, doc_type, office_type, is_jd_book
            ORDER BY chunks DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(r) for r in rows]


def list_policy_doc_types() -> list[dict[str, Any]]:
    with _facts_conn() as c:
        rows = c.execute(
            """
            SELECT COALESCE(doc_type, '') AS doc_type, COUNT(*) AS chunks,
                   SUM(CASE WHEN is_jd_book = 1 THEN 1 ELSE 0 END) AS jd_chunks
            FROM chunks
            GROUP BY COALESCE(doc_type, '')
            ORDER BY chunks DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def search_policies_filtered(
    question: str,
    *,
    top_k: int = 8,
    doc_type: str | None = None,
    office_type: str | None = None,
    jd_only: bool = False,
    doc_name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Keyword search over policy_facts with optional filters; falls back to vector then filter."""
    hits = search_policies(question, top_k=max(top_k * 3, 12))
    out = []
    for h in hits:
        if jd_only and not h.get("is_jd_book"):
            continue
        if doc_type and str(h.get("doc_type") or "").lower() != doc_type.lower():
            continue
        if office_type and str(h.get("office_type") or "").lower() != office_type.lower():
            continue
        if doc_name_contains and doc_name_contains.lower() not in str(h.get("doc_name") or "").lower():
            continue
        out.append(h)
        if len(out) >= top_k:
            break
    if out:
        return out
    # SQLite filtered keyword if vector path had no filter matches
    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", question.lower()) if t not in {"the", "and", "for"}]
    clauses = ["1=1"]
    params: list[Any] = []
    if jd_only:
        clauses.append("is_jd_book = 1")
    if doc_type:
        clauses.append("LOWER(COALESCE(doc_type,'')) = ?")
        params.append(doc_type.lower())
    if office_type:
        clauses.append("LOWER(COALESCE(office_type,'')) = ?")
        params.append(office_type.lower())
    if doc_name_contains:
        clauses.append("LOWER(COALESCE(doc_name,'')) LIKE ?")
        params.append(f"%{doc_name_contains.lower()}%")
    with _facts_conn() as c:
        rows = c.execute(
            f"SELECT id, doc_name, section, text, doc_type, office_type, is_jd_book FROM chunks WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
    scored = []
    for r in rows:
        text = (r["text"] or "").lower()
        score = sum(text.count(t) for t in tokens) if tokens else 1
        if score:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, r in scored[:top_k]:
        r["source"] = "policy_facts_db"
        out.append(r)
    return out


def list_schedule_ii(limit: int = 100) -> list[dict[str, Any]]:
    with _facts_conn() as c:
        rows = c.execute(
            """
            SELECT from_bps, to_bps, min_years, note
            FROM schedule_ii
            ORDER BY from_bps, to_bps
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [dict(r) for r in rows]
