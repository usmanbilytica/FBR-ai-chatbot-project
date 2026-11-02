from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

from app.core.config import get_settings
from app.db.hrms import hrms_execute

log = logging.getLogger(__name__)


@dataclass
class TableCard:
    name: str
    columns: list[str]
    pk: list[str]
    fks: list[dict[str, str]]
    description: str = ""

    def text(self) -> str:
        return " ".join(
            [self.name, self.description, " ".join(self.columns)]
            + [f"{f.get('from_col')}->{f.get('to_table')}.{f.get('to_col')}" for f in self.fks]
        )


def catalog_json_path() -> Path:
    return get_settings().catalog_dir / "schema_catalog.json"


def build_live_catalog(sample_values: bool = False) -> dict[str, Any]:
    tables = hrms_execute(
        """
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA='dbo'
        ORDER BY TABLE_NAME
        """
    )
    cols = hrms_execute(
        """
        SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA='dbo'
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
    )
    colmap: dict[str, list[str]] = {}
    for c in cols:
        colmap.setdefault(c["TABLE_NAME"], []).append(c["COLUMN_NAME"])

    pk_rows = hrms_execute(
        """
        SELECT tc.TABLE_NAME, kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_NAME=kcu.CONSTRAINT_NAME AND tc.TABLE_SCHEMA=kcu.TABLE_SCHEMA
        WHERE tc.CONSTRAINT_TYPE='PRIMARY KEY' AND tc.TABLE_SCHEMA='dbo'
        """
    )
    pks: dict[str, list[str]] = {}
    for r in pk_rows:
        pks.setdefault(r["TABLE_NAME"], []).append(r["COLUMN_NAME"])

    fk_rows = hrms_execute(
        """
        SELECT
          tp.name AS from_table, cp.name AS from_col,
          tr.name AS to_table, cr.name AS to_col
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fk.object_id=fkc.constraint_object_id
        JOIN sys.tables tp ON fkc.parent_object_id=tp.object_id
        JOIN sys.columns cp ON fkc.parent_object_id=cp.object_id AND fkc.parent_column_id=cp.column_id
        JOIN sys.tables tr ON fkc.referenced_object_id=tr.object_id
        JOIN sys.columns cr ON fkc.referenced_object_id=cr.object_id AND fkc.referenced_column_id=cr.column_id
        """
    )
    fks: dict[str, list[dict[str, str]]] = {}
    for r in fk_rows:
        fks.setdefault(r["from_table"], []).append(
            {
                "from_col": r["from_col"],
                "to_table": r["to_table"],
                "to_col": r["to_col"],
            }
        )

    core_boost = {
        "pr_employee_mf": "Employee master hub NICNo EmployeeID grade location org",
        "pr_position_mf": "Position chairs sanctioned strength by count grade designation",
        "pr_designation": "Designation master",
        "pr_designation_sanctioned_allocation": "Designation sanctioned strength by org location",
        "pr_employee_grades_mf": "Grade BPS sanctioned strength",
        "pr_employee_grade_budget_allocation": "Grade budget sanction by OrgLevel2",
        "pr_employee_Action_mf": "Career actions posting transfer deputation history",
        "pr_leave_application": "Leave applications LPR study",
        "pr_suspension_mf": "Suspension",
        "pr_ed_mf": "Efficiency and discipline",
        "pr_inquiry_mf": "Inquiry",
        "hr_training_course_mf": "Training courses",
        "hr_training_course_eligibility_dt": "Training course employee eligibility",
        "pr_employee_seniority": "Seniority",
        "adm_user_company": "RBAC AccessID locations org",
        "adm_org_structure": "Organization structure",
    }

    out_tables = {}
    for t in tables:
        name = t["TABLE_NAME"]
        out_tables[name] = {
            "columns": colmap.get(name, []),
            "pk": pks.get(name, []),
            "fks": fks.get(name, []),
            "description": core_boost.get(name, ""),
            "in_scope": name in core_boost,
        }

    catalog = {
        "table_count": len(out_tables),
        "tables": out_tables,
    }
    path = catalog_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog), encoding="utf-8")
    log.info("Wrote catalog %s tables to %s", len(out_tables), path)
    return catalog


def load_catalog() -> dict[str, Any]:
    path = catalog_json_path()
    if not path.exists():
        return build_live_catalog()
    return json.loads(path.read_text(encoding="utf-8"))


_bm25 = None
_bm25_names: list[str] = []


def _ensure_bm25(catalog: dict[str, Any]) -> None:
    global _bm25, _bm25_names
    if _bm25 is not None:
        return
    names = []
    corpus = []
    for name, meta in catalog.get("tables", {}).items():
        names.append(name)
        desc = meta.get("description") or ""
        boost = " " + desc if meta.get("in_scope") else ""
        doc = f"{name} {' '.join(meta.get('columns') or [])} {desc}{boost}"
        corpus.append(re.findall(r"[a-z0-9_]+", doc.lower()))
    _bm25_names = names
    _bm25 = BM25Okapi(corpus)


def link_tables(question: str, top_k: int = 8, widen: bool = False) -> list[TableCard]:
    catalog = load_catalog()
    _ensure_bm25(catalog)
    assert _bm25 is not None
    tokens = re.findall(r"[a-z0-9_]+", question.lower())
    scores = _bm25.get_scores(tokens)
    k = top_k * 3 if widen else top_k
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    cards: list[TableCard] = []
    tables = catalog["tables"]
    for i in ranked:
        name = _bm25_names[i]
        meta = tables[name]
        cards.append(
            TableCard(
                name=name,
                columns=list(meta.get("columns") or []),
                pk=list(meta.get("pk") or []),
                fks=list(meta.get("fks") or []),
                description=meta.get("description") or "",
            )
        )
    # FK expansion toward hubs
    hubs = {"pr_employee_mf", "pr_position_mf"}
    extra: list[TableCard] = []
    have = {c.name for c in cards}
    for card in list(cards):
        for fk in card.fks:
            tgt = fk.get("to_table")
            if tgt in hubs and tgt not in have and tgt in tables:
                meta = tables[tgt]
                extra.append(
                    TableCard(
                        name=tgt,
                        columns=list(meta.get("columns") or []),
                        pk=list(meta.get("pk") or []),
                        fks=list(meta.get("fks") or []),
                        description=meta.get("description") or "",
                    )
                )
                have.add(tgt)
    return cards + extra


def schema_pack(cards: list[TableCard], max_cols: int = 40) -> str:
    parts = []
    for c in cards:
        cols = c.columns[:max_cols]
        fk = ", ".join(f"{f['from_col']}->{f['to_table']}.{f['to_col']}" for f in c.fks[:12])
        parts.append(
            f"TABLE dbo.{c.name}\n  PK: {c.pk}\n  COLUMNS: {cols}\n  FKS: {fk}\n  NOTE: {c.description}"
        )
    return "\n\n".join(parts)
