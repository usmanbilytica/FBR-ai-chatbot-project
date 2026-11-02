from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.core.config import get_settings
from app.rules.status_map import is_blocked_status, status_ids_for
from app.rules.types import RuleResult, Severity


SERVICE_KEYS = {"IRS", "PCS", "BOARD", "DRS"}


def _bps_num(raw: Any) -> int | None:
    if raw is None:
        return None
    s = str(raw).upper().replace("BS-", "").replace("BPS-", "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else None


def _parse_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:10]).date()
    except Exception:
        return None


def cadre_compatible(officer_service: str | None, seat_service: str | None) -> tuple[Severity, str]:
    o = (officer_service or "").upper().strip()
    s = (seat_service or "").upper().strip()
    if not o or not s:
        return Severity.MISSING, "service key missing for officer or seat"
    if o not in SERVICE_KEYS or s not in SERVICE_KEYS:
        return Severity.NOTE, f"nonstandard service keys officer={o} seat={s}"
    if s == "BOARD" and o in {"IRS", "PCS", "BOARD"}:
        return Severity.PASS, "Board accepts IRS+PCS"
    if s == "DRS" and o != "DRS":
        return Severity.FAIL, "DRS seats are exclusive to DRS"
    if {o, s} == {"IRS", "PCS"}:
        return Severity.FAIL, "IRS ↔ PCS exclusive"
    if o == s:
        return Severity.PASS, "same service"
    if o == "BOARD" and s in {"IRS", "PCS"}:
        return Severity.PASS, "Board officer to cadre seat"
    return Severity.WARNING, f"service pairing officer={o} seat={s}"


def grade_fit(officer_bps: int | None, seat_bps: int | None) -> tuple[Severity, str]:
    if officer_bps is None or seat_bps is None:
        return Severity.MISSING, "BPS missing for grade fit"
    if officer_bps == seat_bps:
        return Severity.PASS, "exact grade match"
    if officer_bps < seat_bps:
        return Severity.FAIL, f"officer BPS {officer_bps} below seat band {seat_bps}"
    if officer_bps == seat_bps + 1:
        return Severity.NOTE, f"one grade above seat → OPS note (officer {officer_bps} vs seat {seat_bps})"
    return Severity.FAIL, f"officer BPS {officer_bps} incompatible with seat {seat_bps}"


def eligibility_transfer(
    officer: dict[str, Any],
    seat: dict[str, Any],
    *,
    purpose: str = "transfer",
    open_ed: bool | None = None,
    open_inquiry: bool | None = None,
    covering_suspension: bool | None = None,
    station_tenure_years: float | None = None,
    rotation_hard: bool = False,
) -> RuleResult:
    reasons: list[str] = []
    status = Severity.PASS
    settings = get_settings()

    ob = _bps_num(officer.get("bps") or officer.get("GradeCode") or officer.get("EmployeeGradeID"))
    sb = _bps_num(seat.get("bps") or seat.get("GradeCode") or seat.get("GradeID"))
    gstat, greason = grade_fit(ob, sb)
    reasons.append(greason)
    if gstat == Severity.FAIL:
        status = Severity.FAIL
    elif gstat == Severity.NOTE and status == Severity.PASS:
        status = Severity.NOTE
    elif gstat == Severity.MISSING:
        status = Severity.MISSING

    cstat, creason = cadre_compatible(officer.get("service"), seat.get("service"))
    reasons.append(creason)
    if cstat == Severity.FAIL:
        status = Severity.FAIL
    elif cstat == Severity.MISSING and status not in (Severity.FAIL,):
        status = Severity.MISSING

    # Cadre cannot take Ex-Cadre seat
    if seat.get("is_excadre") and not officer.get("is_excadre"):
        reasons.append("Cadre on Ex-Cadre seat → NOTE (deputation)")
        if status == Severity.PASS:
            status = Severity.NOTE
    if officer.get("is_excadre") and not seat.get("is_excadre"):
        reasons.append("Ex-Cadre on cadre seat → NOTE (max 1 year without PM approval; no proforma promotion)")
        if status == Severity.PASS:
            status = Severity.NOTE

    # Title / ML
    if officer.get("designation_ml") and seat.get("designation_ml"):
        if str(officer["designation_ml"]).lower() != str(seat["designation_ml"]).lower():
            reasons.append("Designation ML mismatch")
            status = Severity.FAIL
    if officer.get("designation_title") and seat.get("designation_title"):
        ot = str(officer["designation_title"]).lower()
        st = str(seat["designation_title"]).lower()
        if "assistant" in ot and "assistant" not in st and "commissioner" in st:
            reasons.append("Junior/senior title mismatch (e.g. Assistant Commissioner ≠ Commissioner)")
            status = Severity.FAIL

    # Transfer targets positions only
    if not seat.get("position_id"):
        reasons.append("Transfer can only happen on Positions, not designations alone")
        status = Severity.FAIL

    # Status blocks
    blocked, blabel = is_blocked_status(officer.get("StatusID") or officer.get("status_id"))
    if blocked:
        reasons.append(f"BLOCK: {blabel}")
        status = Severity.BLOCK

    loc = officer.get("LocationID") or officer.get("location_id")
    if loc is not None and int(loc) == settings.general_location_id:
        if officer.get("awaiting_posting") or (
            officer.get("StatusID") in status_ids_for("awaiting_posting")
        ):
            reasons.append("LocationID 1 (General) + awaiting-posting = non-postable source")
            status = Severity.BLOCK

    # Suspension
    if covering_suspension:
        reasons.append("Active covering suspension → HARD FAIL on transfer")
        status = Severity.FAIL

    # Discipline
    if purpose == "promotion":
        if open_ed or open_inquiry:
            reasons.append("Open E&D/inquiry → FAIL for promotion purpose")
            status = Severity.FAIL
        elif open_ed is None and open_inquiry is None:
            reasons.append("Disciplinary standing unknown → MISSING_INPUTS")
            if status not in (Severity.FAIL, Severity.BLOCK):
                status = Severity.MISSING
    else:
        if open_ed or open_inquiry:
            reasons.append("Open E&D/inquiry → WARNING on transfer")
            if status == Severity.PASS:
                status = Severity.WARNING

    # Tenure
    if station_tenure_years is not None:
        if station_tenure_years >= settings.rotation_default_years:
            msg = f"Station tenure {station_tenure_years:.1f}y ≥ policy max-stay ({settings.rotation_default_years}y)"
            if rotation_hard:
                reasons.append(msg + " → HARD")
                status = Severity.FAIL
            else:
                reasons.append(msg + " → NOTE")
                if status == Severity.PASS:
                    status = Severity.NOTE

    # Retire soon
    retire = _parse_date(officer.get("RetirementDate"))
    dob = _parse_date(officer.get("DOB"))
    if retire is None and dob is not None:
        try:
            retire = date(dob.year + settings.retirement_age_years, dob.month, dob.day)
        except ValueError:
            retire = date(dob.year + settings.retirement_age_years, dob.month, 28)
    if retire is not None:
        days = (retire - date.today()).days
        if 0 <= days <= settings.retire_soon_days:
            reasons.append(f"Retire within {days} days → EXCLUDE")
            status = Severity.EXCLUDE

    # Education/training bars do NOT apply on transfer
    reasons.append("Transfer does not apply education/training bars")

    return RuleResult(
        status=status,
        reasons=reasons,
        evidence=[{"officer": officer, "seat": seat, "purpose": purpose}],
        citations=["rules:transfer"],
        data={"officer_bps": ob, "seat_bps": sb},
    )
