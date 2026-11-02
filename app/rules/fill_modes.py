from __future__ import annotations

from typing import Any, Literal

from app.core.config import get_settings
from app.rules.types import RuleResult, Severity

FillMode = Literal[
    "permanent", "ops", "tps", "additional", "lookafter", "attachment", "appointment_by_transfer"
]


def _bps(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).upper().replace("BS-", "").replace("BPS-", "")
    d = "".join(c for c in s if c.isdigit())
    return int(d) if d else None


def fill_mode_contract(
    mode: FillMode,
    *,
    seat_vacant: bool | None,
    seat_bps: int | None,
    nominee_bps: int | None,
    occupying_grades: list[int] | None = None,
    same_chair: bool = False,
    same_position_id: bool = False,
) -> RuleResult:
    settings = get_settings()
    reasons: list[str] = []
    status = Severity.PASS
    occupying_grades = occupying_grades or []

    if mode == "appointment_by_transfer":
        return RuleResult(
            Severity.BLOCK,
            reasons=["Appointment by transfer is not a selectable same-cadre fill chip"],
            citations=["rules:fill_modes"],
        )

    if same_position_id:
        reasons.append("Exclude vacating incumbent / same PositionID")
        status = Severity.EXCLUDE

    if mode == "permanent":
        if seat_vacant is False:
            reasons.append("Permanent transfer needs vacant chair (position lens)")
            status = Severity.FAIL
        elif seat_vacant is None:
            reasons.append("Vacancy unknown → MISSING_INPUTS")
            status = Severity.MISSING
        else:
            reasons.append("Vacant destination OK for permanent")
        if nominee_bps is not None and seat_bps is not None and nominee_bps != seat_bps:
            reasons.append("Permanent requires same substantive grade")
            status = Severity.FAIL
        if same_chair:
            reasons.append("Already holds same chair → block permanent")
            status = Severity.BLOCK
        reasons.append("Permanent creates source vacancy")

    elif mode == "ops":
        if seat_bps is None:
            status = Severity.MISSING
            reasons.append("seat BPS missing for OPS")
        elif seat_bps < settings.ops_min_bps:
            status = Severity.FAIL
            reasons.append(f"Seat BPS {seat_bps} < OPS min {settings.ops_min_bps}")
        else:
            reasons.append(f"Seat ≥ OPS min BPS-{settings.ops_min_bps}")
        if nominee_bps is not None and seat_bps is not None and nominee_bps != seat_bps - 1:
            reasons.append(f"OPS nominee must be one below seat (got {nominee_bps}, seat {seat_bps})")
            status = Severity.FAIL
        reasons.append("OPS may fill occupied seat")

    elif mode == "tps":
        if seat_bps is None:
            status = Severity.MISSING
            reasons.append("seat BPS missing for TPS")
        elif seat_bps > settings.tps_max_bps:
            status = Severity.FAIL
            reasons.append(f"Seat BPS {seat_bps} > TPS max {settings.tps_max_bps}")
        else:
            reasons.append(f"Seat ≤ TPS max BPS-{settings.tps_max_bps}")
        if nominee_bps is not None and seat_bps is not None and nominee_bps != seat_bps - 1:
            reasons.append("TPS nominee must be one below seat")
            status = Severity.FAIL

    elif mode == "additional":
        if occupying_grades and nominee_bps is not None and nominee_bps not in occupying_grades:
            reasons.append("Additional charge requires same occupying grade(s)")
            status = Severity.FAIL
        reasons.append(f"Additional charge ~{settings.additional_charge_days} days / 3 months (+ extension NOTE)")
        if status == Severity.PASS:
            status = Severity.NOTE

    elif mode == "lookafter":
        if occupying_grades and nominee_bps is not None:
            lowest = min(occupying_grades)
            if nominee_bps != lowest - 1:
                reasons.append(
                    f"Look-after nominee must be one below lowest occupying grade ({lowest})"
                )
                status = Severity.FAIL
        reasons.append("Look-after is not a Civil Servants Act appointment")

    elif mode == "attachment":
        reasons.append("Attachment: occupied OK; creates source vacancy")

    else:
        return RuleResult(Severity.FAIL, reasons=[f"unknown fill mode {mode}"])

    return RuleResult(
        status=status,
        reasons=reasons,
        citations=[f"rules:fill_modes:{mode}"],
        data={
            "mode": mode,
            "seat_vacant": seat_vacant,
            "seat_bps": seat_bps,
            "nominee_bps": nominee_bps,
        },
    )
