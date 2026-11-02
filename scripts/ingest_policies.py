#!/usr/bin/env python
"""Ingest RAG Documents PDFs into policy_facts.db + Qdrant (data/qdrant_policy).

Uses native text extraction, then OCR (RapidOCR) for scanned/image PDFs so
nothing is silently skipped.

Run with project venv (parallel-safe with PF indexer which uses data/qdrant_pf):
  .\\.venv\\Scripts\\python.exe scripts\\ingest_policies.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.services.pdf_extract import extract_pdf_text


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    parts = re.split(r"\n(?=Section\s+\d+|ARTICLE\s+\d+|Chapter\s+\d+|\d+\.\s+)", text, flags=re.I)
    chunks: list[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) < max_chars:
            buf = f"{buf}\n\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            buf = p[: max_chars * 2]
    if buf:
        chunks.append(buf)
    if not chunks and text.strip():
        for i in range(0, len(text), max_chars):
            chunks.append(text[i : i + max_chars])
    return chunks


def classify_doc(name: str) -> tuple[str, str, int]:
    n = name.lower()
    office = "HQ" if "hq" in n else ("Field" if "field" in n else "")
    is_jd = 1 if any(x in n for x in ("jd", "kpi", "jobdescription", "job description")) else 0
    doc_type = "jd" if is_jd else (
        "estacode" if "estacode" in n else (
            "apt" if "appointment" in n or "apt" in n else (
                "conduct" if "conduct" in n else (
                    "promotion" if "promotion" in n else "policy"
                )
            )
        )
    )
    return doc_type, office, is_jd


def _index_qdrant(settings, db: Path) -> None:
    from app.services.embeddings import embed_texts
    from app.services.qdrant_store import (
        assert_qdrant_client_ok,
        collection_count,
        open_client,
        recreate_collection,
        reset_local_store,
        upsert_points,
    )

    ver = assert_qdrant_client_ok()
    print(f"qdrant-client {ver} OK", flush=True)
    store = Path(settings.qdrant_policy_path)
    print(f"Policy Qdrant store: {store}", flush=True)
    # Dedicated store → safe wipe even while PF indexer runs on qdrant_pf
    reset_local_store(store)
    client = open_client(store)
    coll = settings.policy_collection
    try:
        client = recreate_collection(
            client,
            coll,
            size=384,
            distance="Cosine",
            storage_path=store,
        )
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT id, doc_name, section, text, doc_type, office_type, is_jd_book FROM chunks"
        ).fetchall()
        conn.close()
        batch = 32
        cap = int(settings.index_policy_limit or 0)
        total = len(rows) if cap <= 0 else min(len(rows), cap)
        print(f"Embedding {total} policy chunks…", flush=True)
        for i in range(0, total, batch):
            part = rows[i : i + batch]
            vecs = embed_texts([r[3][:2000] for r in part])
            ids = [int(r[0]) for r in part]
            payloads = [
                {
                    "doc_name": r[1],
                    "section": r[2],
                    "text": r[3][:4000],
                    "doc_type": r[4],
                    "office_type": r[5],
                    "is_jd_book": r[6],
                }
                for r in part
            ]
            upsert_points(client, coll, ids, vecs, payloads)
            print(f"  qdrant upsert {i+len(part)}/{total}", flush=True)
        print(
            f"Qdrant policy index ready — points={collection_count(client, coll)}",
            flush=True,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def _enrich_excadre(settings) -> None:
    try:
        from app.sources.hrms.live_resolve import extract_excadre_hops_from_policies

        extracted = extract_excadre_hops_from_policies()
        gaz_path = settings.excadre_gazettes_path
        gaz = {"hops": [], "default_gazette_pct": 1.0}
        if gaz_path.exists():
            gaz = json.loads(gaz_path.read_text(encoding="utf-8"))
        hops = list(gaz.get("hops") or [])
        seen = {(str(h.get("from", "")).lower(), str(h.get("to", "")).lower()) for h in hops}
        added = 0
        for h in extracted:
            key = (h["from"].lower(), h["to"].lower())
            if key not in seen:
                hops.append(h)
                seen.add(key)
                added += 1
        gaz["hops"] = hops
        gaz["policy_extracted_added"] = added
        gaz_path.write_text(json.dumps(gaz, indent=2), encoding="utf-8")
        print(f"Ex-Cadre hops: {len(hops)} total ({added} newly extracted from policies)", flush=True)
    except Exception as ee:
        print(f"Ex-Cadre hop enrich skipped: {ee}", flush=True)


def main(*, skip_qdrant: bool = False, max_ocr_pages: int = 80) -> None:
    settings = get_settings()
    settings.ensure_dirs()
    get_settings.cache_clear()
    settings = get_settings()

    seed_jd = settings.seed_dir / "jd_authority.json"
    if seed_jd.exists():
        shutil.copy(seed_jd, settings.jd_authority_path)
    seed_gaz = settings.seed_dir / "excadre_gazettes.yaml"
    if seed_gaz.exists():
        data = yaml.safe_load(seed_gaz.read_text(encoding="utf-8"))
        settings.excadre_gazettes_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    db = settings.policy_facts_db
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE chunks (
          id INTEGER PRIMARY KEY,
          doc_name TEXT,
          section TEXT,
          text TEXT,
          doc_type TEXT,
          office_type TEXT,
          is_jd_book INTEGER DEFAULT 0,
          extract_method TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE schedule_ii (
          id INTEGER PRIMARY KEY,
          from_bps INTEGER,
          to_bps INTEGER,
          min_years REAL,
          note TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE ingest_log (
          doc_name TEXT PRIMARY KEY,
          method TEXT,
          pages INTEGER,
          ocr_pages INTEGER,
          chars INTEGER,
          chunks INTEGER,
          note TEXT
        )"""
    )
    seed_sii = settings.seed_dir / "schedule_ii.yaml"
    if seed_sii.exists():
        sii = yaml.safe_load(seed_sii.read_text(encoding="utf-8")) or {}
        for e in sii.get("entries") or []:
            conn.execute(
                "INSERT INTO schedule_ii(from_bps,to_bps,min_years,note) VALUES (?,?,?,?)",
                (e.get("from_bps"), e.get("to_bps"), e.get("min_years"), e.get("note")),
            )

    pdf_dir = settings.rag_documents_dir
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    print(f"Found {len(pdfs)} PDFs (OCR fallback enabled, max_ocr_pages={max_ocr_pages})", flush=True)

    n_chunks = 0
    n_ocr_docs = 0
    n_empty = 0
    seen_hash: set[str] = set()
    for pdf in pdfs:
        base = re.sub(r"_compress$", "", pdf.stem, flags=re.I).lower()
        h = hashlib.md5(base.encode()).hexdigest()
        if h in seen_hash:
            print(f" skip dup {pdf.name}", flush=True)
            continue
        seen_hash.add(h)

        text, meta = extract_pdf_text(pdf, max_ocr_pages=max_ocr_pages)
        method = str(meta.get("method") or "")
        if method.startswith("ocr") or meta.get("ocr_pages"):
            n_ocr_docs += 1

        doc_type, office, is_jd = classify_doc(pdf.name)
        if is_jd and settings.jd_authority_path.exists():
            auth = (json.loads(settings.jd_authority_path.read_text(encoding="utf-8")).get("books") or [])
            for b in auth:
                if any(s.lower() in pdf.name.lower() for s in (b.get("match_filename_substrings") or [])):
                    office = b.get("office_type") or office

        chunks = chunk_text(text)
        kept = 0
        for i, ch in enumerate(chunks):
            if len(ch.strip()) < 40:
                continue
            conn.execute(
                "INSERT INTO chunks(doc_name,section,text,doc_type,office_type,is_jd_book,extract_method) VALUES (?,?,?,?,?,?,?)",
                (pdf.name, f"chunk-{i+1}", ch, doc_type, office, is_jd, method),
            )
            n_chunks += 1
            kept += 1
        note = ""
        if kept == 0:
            n_empty += 1
            note = "no usable text after native+OCR"
        conn.execute(
            "INSERT OR REPLACE INTO ingest_log(doc_name,method,pages,ocr_pages,chars,chunks,note) VALUES (?,?,?,?,?,?,?)",
            (
                pdf.name,
                method,
                int(meta.get("pages") or 0),
                int(meta.get("ocr_pages") or 0),
                len(text or ""),
                kept,
                note,
            ),
        )
        print(
            f"  {pdf.name}: {kept} chunks method={method} "
            f"pages={meta.get('pages')} ocr_pages={meta.get('ocr_pages')}",
            flush=True,
        )
        conn.commit()

    conn.close()
    print(
        f"Wrote {n_chunks} chunks → {db} (ocr_docs={n_ocr_docs}, empty={n_empty})",
        flush=True,
    )

    if skip_qdrant:
        print("Skipping Qdrant (--skip-qdrant)", flush=True)
    else:
        try:
            _index_qdrant(settings, db)
        except Exception as e:
            print(f"Qdrant index skipped: {e}", flush=True)

    _enrich_excadre(settings)
    print("Policy ingest complete", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest RAG policy PDFs (+ OCR for scans)")
    parser.add_argument("--skip-qdrant", action="store_true")
    parser.add_argument(
        "--max-ocr-pages",
        type=int,
        default=80,
        help="Max pages to OCR per scanned PDF (default 80)",
    )
    args = parser.parse_args()
    try:
        main(skip_qdrant=args.skip_qdrant, max_ocr_pages=args.max_ocr_pages)
    except Exception as e:
        print(f"Policy ingest FAILED: {e}", file=sys.stderr)
        sys.exit(1)
