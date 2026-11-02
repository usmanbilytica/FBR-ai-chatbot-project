from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.rules.types import RuleResult, Severity
from app.services.llm import chat_json


def _authority() -> dict[str, Any]:
    path = get_settings().jd_authority_path
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    seed = get_settings().seed_dir / "jd_authority.json"
    if seed.exists():
        return json.loads(seed.read_text(encoding="utf-8"))
    return {
        "books": [
            {"id": "hq_feb_2022", "title": "HQ JD Book February 2022", "office_type": "HQ"},
            {"id": "field_june_2022", "title": "Field JD Book June 2022", "office_type": "Field"},
        ]
    }


def jd_gate(
    *,
    office_type: str,
    jd_doc_id: str | None,
    officer: dict[str, Any],
    seat: dict[str, Any],
    curated_hard: dict[str, Any] | None = None,
    jd_text: str | None = None,
    profile_text: str | None = None,
    run_soft_fit: bool = True,
) -> RuleResult:
    auth = _authority()
    books = auth.get("books") or []
    allowed_ids = {b.get("id") for b in books}
    allowed_by_office = {
        b.get("id") for b in books if str(b.get("office_type", "")).lower() == office_type.lower()
    }
    reasons: list[str] = []
    status = Severity.PASS

    if jd_doc_id and jd_doc_id not in allowed_ids:
        return RuleResult(
            Severity.FAIL,
            reasons=["JD book not in authority set (only HQ Feb 2022 / Field June 2022)"],
            citations=["rules:jd_gate"],
        )
    if jd_doc_id and allowed_by_office and jd_doc_id not in allowed_by_office:
        status = Severity.FAIL
        reasons.append(f"JD book office-type mismatch for {office_type}")
    else:
        reasons.append(f"JD book authority OK for office_type={office_type}")

    curated_hard = curated_hard or {}
    # Hard BPS / service / training codes
    req_bps = curated_hard.get("required_bps")
    if req_bps is not None:
        ob = officer.get("bps")
        try:
            if int(str(ob).replace("BS-", "").replace("BPS-", "")) != int(req_bps):
                status = Severity.FAIL
                reasons.append(f"Curated JD hard BPS {req_bps} not met")
        except Exception:
            status = Severity.MISSING
            reasons.append("Could not compare required_bps")
    req_service = curated_hard.get("required_service")
    if req_service and str(officer.get("service", "")).upper() != str(req_service).upper():
        status = Severity.FAIL
        reasons.append(f"Curated JD hard service {req_service} not met")
    req_train = curated_hard.get("required_training_codes") or []
    have = {str(t).upper() for t in (officer.get("trainings") or [])}
    missing = [t for t in req_train if str(t).upper() not in have]
    if missing:
        status = Severity.FAIL
        reasons.append(f"Curated JD hard training codes missing: {missing}")

    soft: dict[str, Any] | None = None
    if run_soft_fit and status == Severity.PASS and jd_text and profile_text:
        try:
            soft = chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are an HR JD fit scorer. Return JSON "
                            '{"fit":"yes"|"no","rationale":"..."}. '
                            "Do NOT invent hard eligibility bars; only soft contextual fit."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"JD:\n{jd_text[:4000]}\n\nProfile:\n{profile_text[:4000]}",
                    },
                ]
            )
            if str(soft.get("fit", "")).lower() == "no":
                status = Severity.SOFT_NO
                reasons.append(f"Level-B JD soft fit=no: {soft.get('rationale')}")
            else:
                reasons.append(f"Level-B JD soft fit=yes: {soft.get('rationale')}")
        except Exception as e:
            reasons.append(f"Soft fit skipped: {e}")

    return RuleResult(
        status=status,
        reasons=reasons,
        evidence=[{"soft": soft, "authority_books": books}],
        citations=["rules:jd_gate"],
        data={"jd_doc_id": jd_doc_id, "office_type": office_type},
    )
