from __future__ import annotations

from typing import Any, Literal

from app.auth.rbac import AccessScope, can_org_shortlist, can_recommend
from app.rules.fill_modes import FillMode, fill_mode_contract
from app.rules.sanction import sanction_strength
from app.rules.transfer import eligibility_transfer
from app.rules.types import RuleResult, Severity

StationFilter = Literal["internal", "external", "any"]


def succession_search(
    *,
    seat: dict[str, Any],
    candidates: list[dict[str, Any]],
    scope: AccessScope,
    fill_modes: list[FillMode] | None = None,
    station_filter: StationFilter = "any",
    seat_location_id: int | None = None,
) -> RuleResult:
    ok, reason = can_recommend(scope)
    if not ok:
        return RuleResult(Severity.BLOCK, reasons=[reason])
    ok, reason = can_org_shortlist(scope)
    if not ok:
        return RuleResult(Severity.BLOCK, reasons=[reason])

    modes = fill_modes or ["permanent", "additional", "lookafter", "ops", "tps", "attachment"]
    # Vacancy-first for permanent
    grade_id = seat.get("GradeID") or seat.get("grade_id")
    pos_strength = sanction_strength(
        "position",
        grade_id=int(grade_id) if grade_id is not None else None,
        designation_id=int(seat["DesignationID"]) if seat.get("DesignationID") else None,
        location_id=seat_location_id or seat.get("LocationID"),
        org_level2=seat.get("OrgLevel2"),
    )
    vacant = float(pos_strength.data.get("vacant") or 0) > 0
    tracks: dict[str, list[dict[str, Any]]] = {m: [] for m in modes}

    for cand in candidates:
        if station_filter == "internal" and seat_location_id is not None:
            if int(cand.get("LocationID") or -1) != int(seat_location_id):
                continue
        if station_filter == "external" and seat_location_id is not None:
            if int(cand.get("LocationID") or -1) == int(seat_location_id):
                continue

        base = eligibility_transfer(
            cand,
            {
                "bps": seat.get("bps") or seat.get("GradeCode"),
                "service": seat.get("service"),
                "is_excadre": seat.get("is_excadre"),
                "designation_ml": seat.get("designation_ml") or seat.get("ManagementLevelID"),
                "designation_title": seat.get("designation_title") or seat.get("PositionName"),
                "position_id": seat.get("ID") or seat.get("position_id"),
            },
            purpose="transfer",
            open_ed=cand.get("open_ed"),
            open_inquiry=cand.get("open_inquiry"),
            covering_suspension=cand.get("covering_suspension"),
            station_tenure_years=cand.get("station_tenure_years"),
        )
        if base.status in (Severity.FAIL, Severity.BLOCK, Severity.EXCLUDE):
            continue

        for mode in modes:
            if mode == "permanent" and not vacant:
                continue
            fm = fill_mode_contract(
                mode,  # type: ignore[arg-type]
                seat_vacant=vacant,
                seat_bps=int(str(seat.get("bps") or seat.get("GradeCode") or "0").replace("BS-", "").replace("BPS-", "") or 0) or None,
                nominee_bps=int(str(cand.get("bps") or cand.get("GradeCode") or "0").replace("BS-", "").replace("BPS-", "") or 0) or None,
                occupying_grades=seat.get("occupying_grades") or [],
                same_chair=bool(cand.get("same_chair")),
                same_position_id=bool(cand.get("PositionID") and cand.get("PositionID") == seat.get("ID")),
            )
            if fm.status in (Severity.FAIL, Severity.BLOCK, Severity.EXCLUDE):
                continue
            tracks[mode].append(
                {
                    "candidate_id": cand.get("ID") or cand.get("employee_id"),
                    "name": cand.get("name") or f"{cand.get('FirstName','')} {cand.get('LastName','')}".strip(),
                    "transfer": base.to_dict(),
                    "fill_mode": fm.to_dict(),
                }
            )

    if "permanent" in modes and not vacant:
        note = "Vacancy-first: no vacant chair → no permanent recommendation"
    else:
        note = "Succession tracks computed"

    return RuleResult(
        status=Severity.PASS if any(tracks.values()) else Severity.MISSING,
        reasons=[note, pos_strength.reasons[0] if pos_strength.reasons else ""],
        evidence=[{"sanction": pos_strength.to_dict()}],
        citations=["rules:succession"],
        data={"tracks": tracks, "vacant_position": vacant},
    )
