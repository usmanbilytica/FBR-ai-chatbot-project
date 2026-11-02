from __future__ import annotations

import json
from typing import Any

from app.core.config import get_settings
from app.rules.sanction import sanction_strength
from app.rules.types import RuleResult, Severity


def _load_gazettes() -> dict[str, Any]:
    path = get_settings().excadre_gazettes_path
    base: dict[str, Any] = {"hops": [], "default_gazette_pct": 1.0}
    if path.exists():
        try:
            base = json.loads(path.read_text(encoding="utf-8")) or base
        except Exception:
            pass
    else:
        seed = get_settings().seed_dir / "excadre_gazettes.yaml"
        if seed.exists():
            import yaml

            base = yaml.safe_load(seed.read_text(encoding="utf-8")) or base
    # Merge live policy-extracted hops when available
    try:
        from app.sources.hrms.live_resolve import extract_excadre_hops_from_policies

        extracted = extract_excadre_hops_from_policies()
        if extracted:
            hops = list(base.get("hops") or [])
            seen = {(str(h.get("from", "")).lower(), str(h.get("to", "")).lower()) for h in hops}
            for h in extracted:
                key = (h["from"].lower(), h["to"].lower())
                if key not in seen:
                    hops.append(h)
                    seen.add(key)
            base["hops"] = hops
            base["extracted_hop_count"] = len(extracted)
    except Exception:
        pass
    return base


def designation_promo_capacity(
    designation_id: int,
    *,
    org_level2: int | None = None,
    location_id: int | None = None,
    gazette_pct: float | None = None,
) -> RuleResult:
    gaz = _load_gazettes()
    pct = gazette_pct if gazette_pct is not None else float(gaz.get("default_gazette_pct") or 1.0)
    strength = sanction_strength(
        "designation",
        designation_id=designation_id,
        org_level2=org_level2,
        location_id=location_id,
    )
    sanctioned = float(strength.data.get("sanctioned") or 0)
    occupied = float(strength.data.get("occupied") or 0)
    capacity = sanctioned * pct
    vacant = capacity - occupied
    return RuleResult(
        status=Severity.PASS,
        reasons=[
            f"Ex-Cadre capacity={capacity:.2f} (sanctioned {sanctioned} x {pct}) "
            f"occupied={occupied} vacant={vacant:.2f}"
        ],
        evidence=[strength.to_dict()],
        citations=["rules:promotion_excadre:capacity"],
        data={"capacity": capacity, "vacant": vacant, "gazette_pct": pct},
    )


def apply_promo_quota(capacity: float, quota_fraction: float) -> float:
    return max(0.0, capacity * quota_fraction)


def eligibility_promotion_excadre(
    officer: dict[str, Any],
    *,
    from_designation: str,
    to_designation: str,
    designation_id: int | None = None,
) -> RuleResult:
    gaz = _load_gazettes()
    hops = gaz.get("hops") or []
    allowed = False
    matched = None
    for h in hops:
        same_from = str(h.get("from", "")).lower() == from_designation.lower()
        same_to = str(h.get("to", "")).lower() == to_designation.lower()
        if same_from and same_to:
            allowed = True
            matched = h
            break
    hop_source = "policy_extract" if gaz.get("extracted_hop_count") else "seed_or_file"
    if not hops:
        return RuleResult(
            status=Severity.MISSING,
            reasons=["Ex-Cadre gazette hops not loaded — run policy ingest / seed merge"],
            citations=["rules:promotion_excadre"],
            data={"hop_source": hop_source, "hop_count": 0},
        )
    if not allowed:
        return RuleResult(
            status=Severity.FAIL,
            reasons=[f"No gazette hop {from_designation} -> {to_designation}"],
            citations=["rules:promotion_excadre"],
            data={"hop_source": hop_source, "hop_count": len(hops)},
        )
    matched_src = (matched or {}).get("gazette") or hop_source
    reasons = [
        f"Gazette hop allowed: {from_designation} -> {to_designation} (source={matched_src})"
    ]
    cap = None
    if designation_id is not None:
        cap = designation_promo_capacity(designation_id)
        reasons.extend(cap.reasons)
        if cap.data.get("vacant", 0) <= 0:
            return RuleResult(
                status=Severity.FAIL,
                reasons=reasons + ["No Ex-Cadre designation vacancy under gazette %"],
                evidence=[cap.to_dict()],
                citations=["rules:promotion_excadre"],
                data={"hop": matched, "hop_source": matched_src},
            )
    if not officer.get("is_excadre"):
        return RuleResult(
            status=Severity.FAIL,
            reasons=["Cadre officers must not use Ex-Cadre promotion queue"],
            citations=["rules:promotion_excadre"],
        )
    return RuleResult(
        status=Severity.PASS,
        reasons=reasons,
        evidence=[{"hop": matched, "capacity": None if cap is None else cap.to_dict()}],
        citations=["rules:promotion_excadre"],
        data={"hop": matched, "hop_source": matched_src},
    )
