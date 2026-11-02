#!/usr/bin/env python
"""Index PF pages into a dedicated local Qdrant store (streamed).

IMPORTANT: run with the project venv, not system Python:
  .\\.venv\\Scripts\\python.exe scripts\\index_pf_pages.py

Uses data/qdrant_pf (separate from policy store data/qdrant) so Windows
file locks and delete/recreate are reliable.

INDEX_PF_LIMIT=0 (default) → full index via keyset pagination.
--resume continues from data/pf_index_checkpoint.json (skips recreate).
--reset-store wipes data/qdrant_pf before indexing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.db.ocr import ocr_execute
from app.services.embeddings import embed_texts
from app.services.qdrant_store import (
    assert_qdrant_client_ok,
    collection_count,
    open_client,
    recreate_collection,
    reset_local_store,
    upsert_points,
)

CHECKPOINT = ROOT / "data" / "pf_index_checkpoint.json"
PAGE_SIZE = 500
EMBED_BATCH = 32


def _page_text(r: dict) -> str:
    return " ".join(
        filter(
            None,
            [
                r.get("subject_text"),
                r.get("summary"),
                r.get("extracted_text"),
            ],
        )
    )


def _payload(r: dict, text: str) -> dict:
    return {
        "employee_cnic": r.get("employee_cnic"),
        "employee_name": r.get("employee_name"),
        "main_category": r.get("main_category"),
        "document_category": r.get("document_category"),
        "event_type": r.get("event_type"),
        "event_subtype": r.get("event_subtype"),
        "text": text[:4000],
        "file_name": r.get("file_name"),
        "page_no": r.get("page_no"),
    }


def _load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_checkpoint(last_id: int, upserted: int) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(
        json.dumps({"last_id": last_id, "upserted": upserted}, indent=2),
        encoding="utf-8",
    )


def _fetch_batch(last_id: int, page_size: int, remaining: int | None) -> list[dict]:
    top = page_size if remaining is None else min(page_size, remaining)
    if top <= 0:
        return []
    return ocr_execute(
        f"""
        SELECT TOP {int(top)}
          id, employee_cnic, employee_name, main_category, document_category,
          event_type, event_subtype, subject_text, summary,
          LEFT(ISNULL(extracted_text,''), 1500) AS extracted_text,
          file_name, page_no, document_date
        FROM dbo.hr_page_360_dt
        WHERE id > :last_id
          AND extracted_text IS NOT NULL AND LEN(extracted_text) > 40
        ORDER BY id ASC
        """,
        {"last_id": int(last_id)},
    )


def main(
    limit: int | None = None,
    *,
    resume: bool = False,
    reset_store: bool = False,
) -> None:
    settings = get_settings()
    ver = assert_qdrant_client_ok()
    print(f"qdrant-client {ver} OK")

    store = Path(settings.qdrant_pf_path)
    print(f"PF Qdrant store: {store}")

    if limit is None:
        limit = int(settings.index_pf_limit or 0)
    remaining: int | None = int(limit) if limit and limit > 0 else None

    if reset_store or not resume:
        # Dedicated store → safe full wipe (avoids Windows lock fights with policy DB)
        print(f"Resetting PF store at {store}")
        reset_local_store(store)
        resume = False
        if CHECKPOINT.exists():
            CHECKPOINT.unlink()

    last_id = 0
    upserted = 0
    if resume:
        cp = _load_checkpoint()
        last_id = int(cp.get("last_id") or 0)
        upserted = int(cp.get("upserted") or 0)
        print(f"Resuming from last_id={last_id} (already upserted={upserted})")

    client = open_client(store)
    coll = settings.pf_collection
    try:
        if not resume or not client.collection_exists(coll):
            client = recreate_collection(
                client,
                coll,
                size=384,
                distance="Cosine",
                storage_path=store,
            )
            last_id = 0
            upserted = 0
            _save_checkpoint(0, 0)
            n = collection_count(client, coll)
            print(f"Fresh collection {coll} points={n}")
            if n != 0:
                raise RuntimeError(f"Expected empty collection after recreate, got {n}")
        else:
            print(f"Collection {coll} exists with {collection_count(client, coll)} points")

        while True:
            print(f" fetching OCR batch after id={last_id} …", flush=True)
            batch = _fetch_batch(last_id, PAGE_SIZE, remaining)
            if not batch:
                print(" no more OCR rows", flush=True)
                break
            print(f" fetched {len(batch)} rows (ids {batch[0]['id']}..{batch[-1]['id']})", flush=True)
            for i in range(0, len(batch), EMBED_BATCH):
                part = batch[i : i + EMBED_BATCH]
                texts = [_page_text(r)[:2000] for r in part]
                vecs = embed_texts(texts)
                ids = [int(r["id"]) for r in part]
                payloads = [_payload(r, t) for r, t in zip(part, texts)]
                upsert_points(client, coll, ids, vecs, payloads)
                upserted += len(part)
                last_id = int(part[-1]["id"])
                if remaining is not None:
                    remaining -= len(part)
                if upserted % 128 == 0 or i + EMBED_BATCH >= len(batch):
                    _save_checkpoint(last_id, upserted)
                    print(f" upserted {upserted} (last_id={last_id})", flush=True)
            if remaining is not None and remaining <= 0:
                break
            if len(batch) < PAGE_SIZE:
                break

        _save_checkpoint(last_id, upserted)
        final = collection_count(client, coll)
        print(f"PF Qdrant index ready — points={final} upserted={upserted}", flush=True)
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index PF pages into Qdrant (streamed)")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max pages (default: INDEX_PF_LIMIT env; 0 = full)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue from checkpoint without recreating the collection",
    )
    parser.add_argument(
        "--reset-store",
        action="store_true",
        help="Wipe data/qdrant_pf before indexing",
    )
    args = parser.parse_args()
    try:
        main(limit=args.limit, resume=args.resume, reset_store=args.reset_store)
    except Exception as e:
        print(f"PF index FAILED: {e}", file=sys.stderr)
        sys.exit(1)
