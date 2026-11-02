"""Hybrid PF vector search over data/qdrant_pf (best-effort during live indexing)."""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.services.entity_resolution import cnic_digits, cnic_variants
from app.services.embeddings import embed_query

log = logging.getLogger(__name__)


def pf_vector_search(
    question: str,
    *,
    cnics: list[str] | None = None,
    top_k: int = 12,
) -> dict[str, Any]:
    """Search PF Qdrant collection. Never raises — returns status for callers.

    While `index_pf_pages.py` holds the local store lock, this returns
    status=unavailable and empty hits so SQL retrieval can continue.
    """
    settings = get_settings()
    store = settings.qdrant_pf_path
    coll = settings.pf_collection
    out: dict[str, Any] = {
        "status": "unavailable",
        "store": str(store),
        "hits": [],
        "note": "",
    }
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm

        client = QdrantClient(path=str(store))
        try:
            if not client.collection_exists(coll):
                out["note"] = "PF collection not found — index still building or not started"
                return out
            info = client.get_collection(coll)
            points = int(getattr(info, "points_count", 0) or 0)
            if points <= 0:
                out["note"] = "PF collection empty — index in progress"
                return out

            vec = embed_query(question)
            query_filter = None
            if cnics:
                variants: list[str] = []
                for c in cnics:
                    variants.extend(cnic_variants(c))
                    d = cnic_digits(c)
                    if d:
                        variants.append(d)
                variants = list(dict.fromkeys(variants))
                if variants:
                    query_filter = qm.Filter(
                        should=[
                            qm.FieldCondition(
                                key="employee_cnic",
                                match=qm.MatchValue(value=v),
                            )
                            for v in variants[:20]
                        ]
                    )

            # qdrant-client 1.20 uses query_points; keep search() for older
            hits_raw: list[Any] = []
            try:
                if hasattr(client, "query_points"):
                    res = client.query_points(
                        collection_name=coll,
                        query=vec,
                        query_filter=query_filter,
                        limit=top_k,
                        with_payload=True,
                    )
                    hits_raw = list(getattr(res, "points", None) or [])
                else:
                    hits_raw = client.search(
                        collection_name=coll,
                        query_vector=vec,
                        query_filter=query_filter,
                        limit=top_k,
                    )
            except Exception as e:
                # Retry without CNIC filter if payload index missing
                log.debug("PF vector filtered search failed (%s); retry unfiltered", e)
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

            hits: list[dict[str, Any]] = []
            want_digits = {cnic_digits(c) for c in (cnics or []) if cnic_digits(c)}
            for h in hits_raw:
                payload = getattr(h, "payload", None) or {}
                score = getattr(h, "score", None)
                cnic_hit = str(payload.get("employee_cnic") or "")
                if want_digits and cnic_digits(cnic_hit) not in want_digits:
                    # Soft filter when server-side filter unsupported
                    continue
                hits.append(
                    {
                        "score": score,
                        "employee_cnic": payload.get("employee_cnic"),
                        "employee_name": payload.get("employee_name"),
                        "main_category": payload.get("main_category"),
                        "document_category": payload.get("document_category"),
                        "event_type": payload.get("event_type"),
                        "event_subtype": payload.get("event_subtype"),
                        "file_name": payload.get("file_name"),
                        "page_no": payload.get("page_no"),
                        "text": (payload.get("text") or "")[:1500],
                        "source": "qdrant_pf",
                    }
                )
            out["status"] = "ok"
            out["points_indexed"] = points
            out["hits"] = hits
            out["note"] = f"vector hits={len(hits)} from {points} indexed points"
            return out
        finally:
            try:
                client.close()
            except Exception:
                pass
    except Exception as e:
        msg = str(e)
        if "being used by another process" in msg or "WinError 32" in msg or "already accessed" in msg.lower():
            out["note"] = "PF vector store locked by indexer — using SQL only until index finishes"
        else:
            out["note"] = f"PF vector search unavailable: {e}"
        log.debug("pf_vector_search: %s", out["note"])
        return out
