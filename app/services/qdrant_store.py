"""Qdrant local-store helpers compatible with qdrant-client >= 1.14.

The on-disk path `data/qdrant` must always be opened with the project venv
client (currently 1.20.x). Mixing system Python 1.12 with a 1.20 store causes
CreateCollection pydantic validation errors (memory/payload/id_tracker).

Local-mode delete_collection is unreliable — recreate also removes
`collection/<name>` on disk and patches meta.json.
"""
from __future__ import annotations

import importlib.metadata
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

MIN_QDRANT_CLIENT = (1, 14, 0)


def qdrant_client_version() -> tuple[int, ...]:
    raw = importlib.metadata.version("qdrant-client")
    parts: list[int] = []
    for p in raw.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if digits:
            parts.append(int(digits))
        if len(parts) >= 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def assert_qdrant_client_ok() -> str:
    ver = qdrant_client_version()
    ver_s = ".".join(str(x) for x in ver)
    if ver < MIN_QDRANT_CLIENT:
        raise RuntimeError(
            f"qdrant-client {ver_s} is too old for this project's local store. "
            f"Use the project venv (.venv\\Scripts\\python.exe) which has >= "
            f"{'.'.join(str(x) for x in MIN_QDRANT_CLIENT)}. "
            f"System Python often has 1.12.x and fails CreateCollection."
        )
    return ver_s


def open_client(path: Path | str):
    assert_qdrant_client_ok()
    from qdrant_client import QdrantClient

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(path))


def _hard_wipe_local_collection(storage_path: Path, name: str) -> None:
    coll_dir = storage_path / "collection" / name
    if coll_dir.exists():
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                shutil.rmtree(coll_dir)
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
        if last_err is not None or coll_dir.exists():
            raise RuntimeError(
                f"Could not wipe {coll_dir} (file lock?). "
                f"Stop any running API/indexer using data/qdrant, then retry. Last error: {last_err}"
            )
        log.info("Removed on-disk collection dir %s", coll_dir)
    meta_path = storage_path / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cols = meta.get("collections") or {}
            if name in cols:
                cols.pop(name, None)
                meta["collections"] = cols
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
                log.info("Removed %s from meta.json", name)
        except Exception as e:
            log.warning("meta.json patch failed: %s", e)


def recreate_collection(
    client: Any,
    name: str,
    *,
    size: int = 384,
    distance: str = "Cosine",
    storage_path: Path | str | None = None,
) -> Any:
    """Delete + create collection. Returns a (possibly reopened) client.

    For local path stores, pass storage_path so we hard-wipe the collection
    directory — API delete alone often leaves points behind.
    """
    from qdrant_client.http import models as qm

    dist = qm.Distance.COSINE if distance.lower() == "cosine" else qm.Distance(distance)
    path = Path(storage_path) if storage_path is not None else None

    try:
        if client.collection_exists(name):
            client.delete_collection(name)
            log.info("API deleted Qdrant collection %s", name)
    except Exception as e:
        log.warning("delete_collection(%s) failed: %s", name, e)

    if path is not None:
        # Local embedded store: must close before filesystem wipe
        try:
            client.close()
        except Exception:
            pass
        time.sleep(0.2)
        _hard_wipe_local_collection(path, name)
        client = open_client(path)

    client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(size=int(size), distance=dist),
    )
    log.info("Created Qdrant collection %s (dim=%s)", name, size)
    return client


def reset_local_store(path: Path | str) -> None:
    """Nuclear option: wipe the entire local Qdrant directory."""
    path = Path(path)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    log.warning("Reset local Qdrant store at %s", path)


def upsert_points(
    client: Any,
    collection: str,
    ids: Sequence[int],
    vectors: Sequence[Sequence[float]],
    payloads: Sequence[dict[str, Any]],
) -> int:
    from qdrant_client.http import models as qm

    if not ids:
        return 0
    points = [
        qm.PointStruct(id=int(i), vector=list(v), payload=dict(p))
        for i, v, p in zip(ids, vectors, payloads)
    ]
    client.upsert(collection_name=collection, points=points)
    return len(points)


def collection_count(client: Any, name: str) -> int:
    if not client.collection_exists(name):
        return 0
    info = client.get_collection(name)
    return int(getattr(info, "points_count", 0) or 0)
