from __future__ import annotations

import logging
from typing import Any

from app.auth.rbac import AccessScope, redact_topics
from app.db.ocr import ocr_execute
from app.services.entity_resolution import cnic_digits, resolve_officer
from app.sources.personal_files.taxonomy import category_match_sql

log = logging.getLogger(__name__)


def _cnic_where(cnics: list[str], col: str = "employee_cnic") -> tuple[str, dict[str, Any]]:
    if not cnics:
        return "1=0", {}
    params: dict[str, Any] = {}
    parts = []
    for i, c in enumerate(cnics):
        params[f"c{i}"] = c
        parts.append(f"{col} = :c{i}")
    digs = list({cnic_digits(c) for c in cnics if cnic_digits(c)})
    for i, d in enumerate(digs):
        params[f"d{i}"] = d
        parts.append(f"REPLACE(REPLACE(ISNULL({col},''),'-',''),' ','') = :d{i}")
    return "(" + " OR ".join(parts) + ")", params


def pf_resolve_officer(
    *,
    employee_id: int | None = None,
    cnic: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    ref = resolve_officer(employee_id=employee_id, cnic=cnic, name=name)
    return {
        "hrms_employee_id": ref.hrms_employee_id,
        "cnic": ref.cnic,
        "cnics": ref.cnics,
        "name": ref.name,
        "pf_exists": ref.pf_exists,
        "hrms_row": ref.hrms_row,
    }


def pf_profile_360(cnics: list[str], scope: AccessScope | None = None) -> dict[str, Any] | None:
    where, params = _cnic_where(cnics)
    rows = ocr_execute(
        f"""
        SELECT TOP 1 *
        FROM dbo.hr_employee_360_mf
        WHERE {where}
        ORDER BY updated_at DESC
        """,
        params,
    )
    if not rows:
        return None
    row = rows[0]
    if scope:
        # Drop sensitive text blobs for restricted scopes
        topics = redact_topics(scope, ["medical", "family", "discipline", "complaint", "inquiry"])
        blocked = {"medical", "family", "discipline", "complaint", "inquiry", "ed"} - set(topics)
        for b in blocked:
            for k in list(row.keys()):
                if b in k.lower():
                    row[k] = None
    return row


def pf_list_categories(cnics: list[str]) -> list[dict[str, Any]]:
    where, params = _cnic_where(cnics)
    return ocr_execute(
        f"""
        SELECT TOP 100 main_category AS category, COUNT(*) AS n
        FROM dbo.hr_page_360_dt
        WHERE {where}
        GROUP BY main_category
        ORDER BY COUNT(*) DESC
        """,
        params,
    )


def pf_events(
    cnics: list[str],
    *,
    topics: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where, params = _cnic_where(cnics)
    cat_sql, cat_params = category_match_sql("main_category", topics or [])
    params.update(cat_params)
    return ocr_execute(
        f"""
        SELECT TOP {int(limit)}
          id, employee_cnic, employee_name, event_type, event_subtype, event_title,
          main_category, document_category, event_date, document_date, summary,
          charges_summary, outcome, leave_type, from_office, to_office, designation, bs_grade
        FROM dbo.hr_employee_event_timeline_dt
        WHERE {where} AND {cat_sql}
        ORDER BY COALESCE(event_date, document_date) DESC
        """,
        params,
    )


def pf_pages(
    cnics: list[str],
    *,
    topics: list[str] | None = None,
    query_text: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    where, params = _cnic_where(cnics)
    cat_sql, cat_params = category_match_sql("main_category", topics or [])
    params.update(cat_params)
    text_sql = "1=1"
    if query_text:
        params["q"] = f"%{query_text.lower()}%"
        text_sql = """(
          LOWER(ISNULL(extracted_text,'')) LIKE :q
          OR LOWER(ISNULL(summary,'')) LIKE :q
          OR LOWER(ISNULL(subject_text,'')) LIKE :q
        )"""
    return ocr_execute(
        f"""
        SELECT TOP {int(limit)}
          id, file_360_id, file_name, page_no, employee_cnic, employee_name,
          main_category, document_category, event_type, event_subtype,
          document_group, case_group, leave_type, document_date,
          subject_text, summary,
          LEFT(ISNULL(extracted_text,''), 800) AS extracted_text
        FROM dbo.hr_page_360_dt
        WHERE {where} AND {cat_sql} AND {text_sql}
        ORDER BY document_date DESC, id DESC
        """,
        params,
    )


def pf_segments(
    cnics: list[str],
    *,
    topics: list[str] | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    where, params = _cnic_where(cnics)
    cat_sql, cat_params = category_match_sql("main_category", topics or [])
    params.update(cat_params)
    return ocr_execute(
        f"""
        SELECT TOP {int(limit)}
          id, file_360_id, file_name, employee_cnic, page_start, page_end,
          main_category, document_category, event_type, event_subtype,
          document_group, case_group, subject_text, summary, document_date
        FROM dbo.hr_document_segment_360_dt
        WHERE {where} AND {cat_sql}
        ORDER BY document_date DESC
        """,
        params,
    )


def pf_files(cnics: list[str], limit: int = 20) -> list[dict[str, Any]]:
    where, params = _cnic_where(cnics)
    return ocr_execute(
        f"""
        SELECT TOP {int(limit)}
          id, file_name, original_file_name, employee_cnic, employee_name,
          total_pages, processed_pages, main_pdf_category, file_summary_text, pdf_link
        FROM dbo.hr_file_360_mf
        WHERE {where}
        ORDER BY updated_at DESC
        """,
        params,
    )


def pf_dossier(
    cnics: list[str],
    topics: list[str],
    scope: AccessScope | None = None,
) -> dict[str, Any]:
    if scope:
        topics = redact_topics(scope, topics)
    return {
        "profile": pf_profile_360(cnics, scope),
        "categories": pf_list_categories(cnics),
        "events": pf_events(cnics, topics=topics, limit=40),
        "pages": pf_pages(cnics, topics=topics, limit=20),
        "segments": pf_segments(cnics, topics=topics, limit=20),
        "files": pf_files(cnics),
        "topics": topics,
    }


def pf_search_for_query(
    question: str,
    *,
    employee_id: int | None = None,
    cnic: str | None = None,
    name: str | None = None,
    scope: AccessScope | None = None,
) -> dict[str, Any]:
    from app.auth.rbac import employee_in_scope

    resolved = pf_resolve_officer(employee_id=employee_id, cnic=cnic, name=name)
    if scope and resolved.get("hrms_row"):
        if not employee_in_scope(scope, resolved["hrms_row"]):
            return {
                "resolved": resolved,
                "note": "Officer outside your AccessScope — Personal File withheld",
                "pages": [],
                "events": [],
                "denied": True,
            }
    if scope and scope.access_id == 3 and scope.employee_id is not None:
        # Self scope may only read own PF
        hid = resolved.get("hrms_employee_id")
        if hid is not None and int(hid) != int(scope.employee_id):
            return {
                "resolved": resolved,
                "note": "Self scope may only access own Personal File",
                "pages": [],
                "events": [],
                "denied": True,
            }
    if not resolved.get("cnics"):
        return {"resolved": resolved, "note": "No CNIC resolved for PF search", "pages": [], "events": []}
    if not resolved.get("pf_exists"):
        return {
            "resolved": resolved,
            "note": "No Personal File found for this officer in OCR",
            "pages": [],
            "events": [],
        }
    cnics = resolved["cnics"]
    q = question.lower()
    topics: list[str] = []
    for key in (
        "leave",
        "deputation",
        "promotion",
        "inquiry",
        "complaint",
        "discipline",
        "training",
        "noc",
        "posting",
        "transfer",
        "suspension",
        "medical",
        "family",
    ):
        if key in q:
            topics.append("posting" if key == "transfer" else ("discipline" if key == "discipline" else key))
    if scope:
        topics = redact_topics(scope, topics) if topics else list(
            redact_topics(scope, ["leave", "deputation", "promotion", "training", "noc", "posting", "inquiry", "complaint", "discipline", "medical", "family"])
        )
        # If question asked for blocked topics only, still search but strip sensitive categories
    pages = pf_pages(cnics, topics=topics or None, query_text=question, limit=15)
    if not pages:
        pages = pf_pages(cnics, query_text=question, limit=15)
    events = pf_events(cnics, topics=topics or None, limit=15)

    # Hybrid: vector hits from qdrant_pf (best-effort if indexer holds the lock)
    from app.sources.personal_files.vector_search import pf_vector_search

    vector = pf_vector_search(question, cnics=cnics, top_k=12)
    vector_hits = list(vector.get("hits") or [])

    # Redact sensitive fields from page/event/vector payloads for restricted scopes
    if scope:
        allowed = set(redact_topics(scope, ["medical", "family", "discipline", "complaint", "inquiry", "ed"]))
        blocked = {"medical", "family", "discipline", "complaint", "inquiry", "ed"} - allowed

        def _scrub(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for row in rows:
                cat = str(
                    row.get("main_category") or row.get("event_type") or row.get("document_category") or ""
                ).lower()
                if any(b in cat for b in blocked):
                    continue
                clean = dict(row)
                for b in blocked:
                    for k in list(clean.keys()):
                        if b in k.lower():
                            clean[k] = None
                out.append(clean)
            return out

        pages = _scrub(pages)
        events = _scrub(events)
        vector_hits = _scrub(vector_hits)

    # Merge vector snippets into pages list (dedupe by text prefix)
    seen = {(str(p.get("file_name")), p.get("page_no"), str(p.get("summary") or p.get("text") or "")[:80]) for p in pages}
    for h in vector_hits:
        key = (str(h.get("file_name")), h.get("page_no"), str(h.get("text") or "")[:80])
        if key in seen:
            continue
        pages.append(
            {
                "file_name": h.get("file_name"),
                "page_no": h.get("page_no"),
                "employee_cnic": h.get("employee_cnic"),
                "employee_name": h.get("employee_name"),
                "main_category": h.get("main_category"),
                "document_category": h.get("document_category"),
                "event_type": h.get("event_type"),
                "summary": h.get("text"),
                "extracted_text": h.get("text"),
                "score": h.get("score"),
                "source": "qdrant_pf",
            }
        )
        seen.add(key)

    return {
        "resolved": resolved,
        "topics": topics,
        "categories": pf_list_categories(cnics),
        "pages": pages,
        "events": events,
        "files": pf_files(cnics),
        "vector": {
            "status": vector.get("status"),
            "note": vector.get("note"),
            "points_indexed": vector.get("points_indexed"),
            "hit_count": len(vector_hits),
        },
    }
