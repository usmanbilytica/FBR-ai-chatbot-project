from __future__ import annotations

from typing import Any

from app.rules.sanction import sanction_strength
from app.rules.types import RuleResult, Severity

MANDATORY_TRAINING = {
    19: {"MCMC"},
    20: {"SMC"},
    21: {"NMC", "NSWC", "NDC"},
}


def _bps(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).upper().replace("BS-", "").replace("BPS-", "")
    d = "".join(c for c in s if c.isdigit())
    return int(d) if d else None


def eligibility_promotion_cadre(
    officer: dict[str, Any],
    *,
    target_bps: int,
    completed_trainings: list[str] | None = None,
    qualifying_service_years_bs17: float | None = None,
    required_service_years: float | None = None,
    open_ed_accused: bool = False,
    open_inquiry: bool = False,
    covering_block: bool = False,
    resignation_in_process: bool = False,
    schedule_ii_ok: bool | None = None,
) -> RuleResult:
    reasons: list[str] = []
    status = Severity.PASS
    current = _bps(officer.get("bps") or officer.get("GradeCode"))
    completed_trainings = [t.upper() for t in (completed_trainings or [])]

    if current is None:
        return RuleResult(Severity.MISSING, reasons=["current BPS missing"])
    if target_bps > 21:
        return RuleResult(Severity.FAIL, reasons=["Path ceiling: cannot promote above BPS-21 walk"])
    if target_bps != current + 1:
        reasons.append(f"One-grade steps only (current {current} → target {target_bps})")
        status = Severity.FAIL
    else:
        reasons.append(f"One-grade step {current} → {target_bps} OK")

    # Dual track note
    if officer.get("is_excadre"):
        reasons.append("Ex-Cadre officer must use Ex-Cadre promotion track — never share cadre queue")
        status = Severity.FAIL

    # Service years
    if qualifying_service_years_bs17 is None or required_service_years is None:
        reasons.append("Service years (BS-17+ cumulative − EOL) missing → MISSING_INPUTS")
        if status != Severity.FAIL:
            status = Severity.MISSING
    elif qualifying_service_years_bs17 < required_service_years:
        reasons.append(
            f"Qualifying service {qualifying_service_years_bs17} < required {required_service_years}"
        )
        status = Severity.FAIL
    else:
        reasons.append("Schedule-II / policy service years satisfied")
    if schedule_ii_ok is False:
        reasons.append("Schedule-II lock-in not met")
        status = Severity.FAIL

    # Mandatory training into target
    need = MANDATORY_TRAINING.get(target_bps)
    if need:
        if not (set(completed_trainings) & need):
            reasons.append(f"Mandatory training into {target_bps}: need one of {sorted(need)}")
            status = Severity.FAIL
        else:
            reasons.append(f"Mandatory training satisfied for BPS-{target_bps}")

    # Promo bars
    if open_ed_accused or open_inquiry or covering_block or resignation_in_process:
        reasons.append(
            "Promo bar: open E&D accused / inquiry / covering suspension|deputation|leave / resignation"
        )
        status = Severity.FAIL

    # Vacancy N grade lens
    gid = officer.get("target_grade_id") or officer.get("EmployeeGradeID")
    vac = sanction_strength("grade", grade_id=int(gid) if gid is not None else None)
    reasons.extend(vac.reasons)
    if vac.data.get("vacant", 0) <= 0 and vac.status == Severity.PASS:
        reasons.append("No grade-lens vacancy N for cadre promotion")
        if status == Severity.PASS:
            status = Severity.WARNING

    return RuleResult(
        status=status,
        reasons=reasons,
        evidence=[{"vacancy": vac.to_dict()}],
        citations=["rules:promotion_cadre"],
        data={"current_bps": current, "target_bps": target_bps},
    )
