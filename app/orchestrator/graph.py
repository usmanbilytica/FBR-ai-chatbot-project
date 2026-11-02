"""Orchestrator — agentic multi-source explorer is the default pipeline.

Legacy `route_intent` remains for tests / AGENTIC_EXPLORER=0 fallback only.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

from app.auth.rbac import AccessScope, can_recommend
from app.core.config import get_settings
from app.orchestrator.agent import run_agent
from app.rules import (
    eligibility_promotion_cadre,
    eligibility_promotion_excadre,
    eligibility_training_nomination,
    jd_gate,
    sanction_strength,
    succession_search,
)
from app.services.llm import chat_completion, chat_json
from app.sources.hrms.live_resolve import (
    completed_trainings_for,
    discipline_flags_from_pf,
    fetch_candidates_live,
    fetch_course_live,
    fetch_officer_live,
    fetch_seat_live,
    qualifying_service_years,
    resolve_designation_id,
    resolve_grade_id,
    schedule_ii_lookup,
)
from app.sources.hrms.text2sql import ask_hrms
from app.sources.personal_files import pf_search_for_query
from app.sources.policies import search_policies

log = logging.getLogger(__name__)


def _scope_from_dict(d: dict[str, Any] | None) -> AccessScope:
    d = d or {}
    return AccessScope(
        access_id=d.get("access_id"),
        user_id=d.get("user_id"),
        employee_id=d.get("employee_id"),
        company_id=d.get("company_id"),
        location_ids=list(d.get("location_ids") or []),
        org_structure_ids=list(d.get("org_structure_ids") or []),
        denied=bool(d.get("denied")),
        deny_reason=d.get("deny_reason") or "",
    )


_CNIC_RE = re.compile(r"\b(\d{5}-\d{7}-\d|\d{13})\b")
_ALLOWED_RULES = frozenset(
    {"sanction", "succession", "promotion", "training", "transfer", "jd"}
)


def _extract_cnic(question: str) -> str | None:
    m = _CNIC_RE.search(question or "")
    return m.group(1) if m else None


def _empty_route() -> dict[str, Any]:
    return {
        "hrms": False,
        "pf": False,
        "policy": False,
        "rules": [],
        "topic": "general",
        "employee_name": None,
        "cnic": None,
        "lens": None,
        "grade_id": None,
        "designation_id": None,
        "position_id": None,
        "target_bps": None,
        "from_designation": None,
        "to_designation": None,
        "course_name": None,
        "office_type": None,
        "routing": "llm",
    }


def _apply_safety_nets(question: str, needs: dict[str, Any]) -> dict[str, Any]:
    """Legacy safety nets for AGENTIC_EXPLORER=0 only."""
    cnic = needs.get("cnic") or _extract_cnic(question)
    if cnic:
        needs["cnic"] = cnic
        needs["pf"] = True
        needs["hrms"] = True
    if not needs["hrms"] and not needs["pf"] and not needs["policy"] and not needs["rules"]:
        needs["hrms"] = True
        needs["policy"] = True
        needs["topic"] = needs.get("topic") or "general"
    if needs.get("rules"):
        needs["rules"] = [r for r in needs["rules"] if r in _ALLOWED_RULES]
    return needs


def _fallback_route(question: str) -> dict[str, Any]:
    q = (question or "").lower()
    needs = _empty_route()
    needs["routing"] = "fallback"
    if any(
        k in q
        for k in (
            "report to",
            "reports to",
            "reporting line",
            "job description",
            " jd",
            "jd ",
            "estacode",
            "apt rules",
            "conduct rules",
            "policy",
        )
    ):
        needs["policy"] = True
        needs["topic"] = "policy"
    if any(k in q for k in ("sanction", "strength", "vacancy", "vacant", "occupied")):
        needs["rules"] = ["sanction"]
        needs["hrms"] = True
        needs["topic"] = "sanction"
    if any(k in q for k in ("personal file", "e&d", "inquiry", "dossier")):
        needs["pf"] = True
    if any(k in q for k in ("promot", "mcmc", "smc", "nmc")):
        needs["rules"] = list({*needs["rules"], "promotion"})
        needs["hrms"] = True
        needs["policy"] = True
        needs["topic"] = "promotion"
    if any(k in q for k in ("transfer", "succession", "ops", "tps", "look-after", "lookafter")):
        needs["rules"] = list({*needs["rules"], "succession"})
        needs["hrms"] = True
        needs["topic"] = "succession"
    return _apply_safety_nets(question, needs)


def route_intent(question: str) -> dict[str, Any]:
    """Legacy LLM source flags — unused when AGENTIC_EXPLORER=1 (default)."""
    needs = _empty_route()
    try:
        llm_route = chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You classify FBR HR Decision Support questions. Return ONLY JSON:\n"
                        '{"hrms":bool,"pf":bool,"policy":bool,"rules":[...],'
                        '"topic":str,"employee_name":str|null,"cnic":str|null,'
                        '"lens":"grade"|"designation"|"position"|null,'
                        '"grade_id":int|null,"designation_id":int|null,'
                        '"position_id":int|null,"target_bps":int|null,'
                        '"from_designation":str|null,"to_designation":str|null,'
                        '"course_name":str|null,"office_type":"HQ"|"Field"|null}\n'
                        "rules subset of: sanction,succession,promotion,training,transfer,jd"
                    ),
                },
                {"role": "user", "content": question},
            ]
        )
        if not isinstance(llm_route, dict):
            raise ValueError("LLM route was not a JSON object")
        needs["hrms"] = bool(llm_route.get("hrms"))
        needs["pf"] = bool(llm_route.get("pf"))
        needs["policy"] = bool(llm_route.get("policy"))
        raw_rules = llm_route.get("rules") or []
        if isinstance(raw_rules, list):
            needs["rules"] = [str(x) for x in raw_rules if str(x) in _ALLOWED_RULES]
        needs["topic"] = llm_route.get("topic") or (
            needs["rules"][0] if needs["rules"] else "general"
        )
        for k in (
            "employee_name",
            "cnic",
            "lens",
            "grade_id",
            "designation_id",
            "position_id",
            "target_bps",
            "from_designation",
            "to_designation",
            "course_name",
            "office_type",
        ):
            needs[k] = llm_route.get(k)
        needs["routing"] = "llm"
        return _apply_safety_nets(question, needs)
    except Exception as e:
        log.warning("LLM route failed; using fallback: %s", e)
        return _fallback_route(question)


def _run_sanction(question: str, route: dict[str, Any]) -> Any:
    lens = route.get("lens") or "position"
    ql = question.lower()
    if "grade" in ql or "bps" in ql or "bs-" in ql:
        lens = route.get("lens") or "grade"
    if "designation" in ql:
        lens = route.get("lens") or "designation"
    grade_id = route.get("grade_id")
    designation_id = route.get("designation_id")
    if grade_id is None and lens in ("grade", "position"):
        g = resolve_grade_id(question=question)
        if g:
            grade_id = int(g["ID"])
            route["grade_id"] = grade_id
    if designation_id is None and lens == "designation":
        d = resolve_designation_id(question=question, designation_id=designation_id)
        if d:
            designation_id = int(d["ID"])
            route["designation_id"] = designation_id
    return sanction_strength(
        lens,  # type: ignore[arg-type]
        grade_id=grade_id,
        designation_id=designation_id,
    )


def _run_succession(question: str, route: dict[str, Any], scope: AccessScope, pf_res: dict[str, Any]) -> dict[str, Any]:
    ok, reason = can_recommend(scope)
    if not ok:
        from app.rules.types import RuleResult, Severity

        rr = RuleResult(Severity.BLOCK, reasons=[reason])
        return {"succession": rr.to_dict(), "trace": [rr.to_dict()]}
    seat = fetch_seat_live(
        position_id=route.get("position_id"),
        grade_id=route.get("grade_id"),
        designation_id=route.get("designation_id"),
        question=question,
    )
    if not seat:
        g = resolve_grade_id(question=question, grade_id=route.get("grade_id"))
        if g:
            seat = fetch_seat_live(grade_id=int(g["ID"]), question=question)
    if not seat:
        from app.rules.types import RuleResult, Severity

        rr = RuleResult(
            Severity.MISSING,
            reasons=["No live position/seat resolved from HRMS for this question"],
        )
        return {"succession": rr.to_dict(), "seat": None, "trace": [rr.to_dict()]}
    candidates = fetch_candidates_live(seat, scope=scope, limit=40)
    named = None
    if route.get("cnic") or route.get("employee_name") or scope.employee_id:
        named = fetch_officer_live(
            cnic=route.get("cnic"),
            name=route.get("employee_name"),
            employee_id=scope.employee_id if scope.access_id == 3 else None,
            scope=scope,
        )
        if named and named.get("ID"):
            ids = {c.get("ID") for c in candidates}
            if named["ID"] not in ids:
                flags = discipline_flags_from_pf(named.get("cnics") or [])
                named.update(flags)
                candidates = [named] + candidates
    rr = succession_search(
        seat=seat,
        candidates=candidates,
        scope=scope,
        seat_location_id=seat.get("LocationID"),
    )
    out: dict[str, Any] = {
        "succession": rr.to_dict(),
        "seat": {
            k: seat.get(k)
            for k in ("ID", "PositionName", "bps", "GradeID", "vacant", "designation_title", "LocationID")
        },
        "candidates_considered": len(candidates),
        "trace": [rr.to_dict()],
    }
    if pf_res.get("events"):
        out["discipline_pf_events"] = len(pf_res["events"])
    if named:
        out["named_officer"] = {
            "id": named.get("ID"),
            "name": named.get("name"),
            "bps": named.get("bps"),
        }
    return out


def _run_promotion(question: str, route: dict[str, Any], scope: AccessScope) -> dict[str, Any]:
    officer = fetch_officer_live(
        cnic=route.get("cnic"),
        name=route.get("employee_name"),
        employee_id=scope.employee_id
        if (scope.access_id == 3 or not (route.get("cnic") or route.get("employee_name")))
        else None,
        scope=scope,
    )
    if not officer:
        from app.rules.types import RuleResult, Severity

        rr = RuleResult(Severity.MISSING, reasons=["Officer identity required for promotion eligibility"])
        return {"promotion": rr.to_dict(), "trace": [rr.to_dict()]}
    current = officer.get("bps")
    target = route.get("target_bps")
    if target is None and current is not None:
        target = int(current) + 1
    if target is None:
        from app.rules.types import RuleResult, Severity

        rr = RuleResult(Severity.MISSING, reasons=["Could not determine current/target BPS"])
        return {"promotion": rr.to_dict(), "officer_id": officer.get("ID"), "trace": [rr.to_dict()]}
    flags = discipline_flags_from_pf(officer.get("cnics") or [])
    trainings = completed_trainings_for(int(officer["ID"])) if officer.get("ID") else []
    svc = qualifying_service_years(int(officer["ID"])) if officer.get("ID") else None
    sched = (
        schedule_ii_lookup(int(current), int(target))
        if current is not None and target is not None
        else {"min_years": None, "status": "MISSING"}
    )
    required = sched.get("min_years")
    if officer.get("is_excadre") or route.get("from_designation"):
        rr = eligibility_promotion_excadre(
            officer,
            from_designation=route.get("from_designation") or officer.get("designation_title") or "",
            to_designation=route.get("to_designation") or "",
            designation_id=route.get("designation_id") or officer.get("DesignationID"),
        )
    else:
        tg = resolve_grade_id(bps=int(target))
        if tg:
            officer = {**officer, "target_grade_id": int(tg["ID"])}
        rr = eligibility_promotion_cadre(
            officer,
            target_bps=int(target),
            completed_trainings=trainings,
            qualifying_service_years_bs17=svc,
            required_service_years=required,
            open_ed_accused=flags.get("open_ed", False),
            open_inquiry=flags.get("open_inquiry", False),
            covering_block=flags.get("covering_suspension", False),
            schedule_ii_ok=True if sched.get("status") == "ok" else None,
        )
    return {
        "promotion": rr.to_dict(),
        "officer": {
            "id": officer.get("ID"),
            "name": officer.get("name"),
            "bps": current,
            "is_excadre": officer.get("is_excadre"),
            "service": officer.get("service"),
        },
        "target_bps": target,
        "schedule_ii": sched,
        "discipline": flags,
        "trace": [rr.to_dict()],
    }


def _run_training(question: str, route: dict[str, Any], scope: AccessScope) -> dict[str, Any]:
    officer = fetch_officer_live(
        cnic=route.get("cnic"),
        name=route.get("employee_name"),
        employee_id=scope.employee_id
        if (scope.access_id == 3 or not (route.get("cnic") or route.get("employee_name")))
        else None,
        scope=scope,
    )
    course = fetch_course_live(question=question, course_name=route.get("course_name"))
    if not officer:
        from app.rules.types import RuleResult, Severity

        rr = RuleResult(Severity.MISSING, reasons=["Officer identity required"])
        return {"training": rr.to_dict(), "trace": [rr.to_dict()]}
    if not course:
        from app.rules.types import RuleResult, Severity

        rr = RuleResult(Severity.MISSING, reasons=["Course not resolved"])
        return {"training": rr.to_dict(), "officer_id": officer.get("ID"), "trace": [rr.to_dict()]}
    prior = completed_trainings_for(int(officer["ID"])) if officer.get("ID") else []
    already = any(
        str(course.get("CourseName") or "").upper() in t or t in str(course.get("CourseName") or "").upper()
        for t in prior
    )
    rr = eligibility_training_nomination(
        officer, course, prior_class3_certs=prior, already_completed=already
    )
    return {
        "training": rr.to_dict(),
        "officer": {"id": officer.get("ID"), "name": officer.get("name")},
        "course": course,
        "trace": [rr.to_dict()],
    }


def _run_jd(question: str, route: dict[str, Any], scope: AccessScope, policy_res: list[dict[str, Any]]) -> dict[str, Any]:
    officer = (
        fetch_officer_live(
            cnic=route.get("cnic"),
            name=route.get("employee_name"),
            employee_id=scope.employee_id,
            scope=scope,
        )
        or {}
    )
    office = route.get("office_type") or ("HQ" if "hq" in question.lower() else "Field")
    jd_text = None
    jd_doc_id = None
    for p in policy_res:
        if p.get("is_jd_book") or (p.get("doc_type") == "jd"):
            jd_text = p.get("text")
            name = (p.get("doc_name") or "").lower()
            if "hq" in name:
                jd_doc_id = "hq_feb_2022"
            elif "field" in name:
                jd_doc_id = "field_june_2022"
            break
    if jd_doc_id is None:
        jd_doc_id = "hq_feb_2022" if office == "HQ" else "field_june_2022"
    seat = fetch_seat_live(question=question, grade_id=route.get("grade_id")) or {}
    trainings = completed_trainings_for(int(officer["ID"])) if officer.get("ID") else []
    officer_j = {**officer, "trainings": trainings}
    rr = jd_gate(
        office_type=office,
        jd_doc_id=jd_doc_id,
        officer=officer_j,
        seat=seat,
        jd_text=jd_text,
        profile_text=str(officer_j.get("name") or "") + f" BPS-{officer_j.get('bps')}",
        run_soft_fit=bool(jd_text and officer),
    )
    return {"jd": rr.to_dict(), "trace": [rr.to_dict()]}


def _run_legacy_pipeline(
    question: str,
    scope: AccessScope,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Pre-agent route_intent pipeline (AGENTIC_EXPLORER=0)."""

    def step(sid: str, label: str, status: str, detail: Any = None) -> None:
        if on_step:
            on_step({"id": sid, "label": label, "status": status, "detail": detail})

    route = route_intent(question)
    step("route", "Routing intent (legacy)", "done", {k: route.get(k) for k in ("topic", "rules", "hrms", "pf", "policy")})
    evidence: list[dict[str, Any]] = []
    rule_trace: list[dict[str, Any]] = []
    hrms_res: dict[str, Any] = {}
    pf_res: dict[str, Any] = {}
    policy_res: list[dict[str, Any]] = []
    rules_res: dict[str, Any] = {}

    if route.get("pf"):
        step("pf", "Personal Files retrieval", "running")
        pf_res = pf_search_for_query(
            question,
            cnic=route.get("cnic"),
            name=route.get("employee_name"),
            employee_id=scope.employee_id if scope.access_id == 3 else None,
            scope=scope,
        )
        evidence.append({"source": "personal_files", "data": pf_res})
        step("pf", "Personal Files retrieval", "done")

    if route.get("policy"):
        step("policy", "Policy / JD retrieval", "running")
        policy_res = search_policies(question)
        evidence.append({"source": "policies", "data": policy_res})
        step("policy", "Policy / JD retrieval", "done")

    for rule in route.get("rules") or []:
        step(f"rule_{rule}", f"Rule engine: {rule}", "running")
        try:
            if rule == "sanction":
                rr = _run_sanction(question, route)
                if rr.status.value == "MISSING_INPUTS":
                    hrms_res = ask_hrms(question, scope=scope)
                    evidence.append({"source": "hrms_sql", "data": hrms_res})
                rules_res["sanction"] = rr.to_dict()
                rule_trace.append(rr.to_dict())
            elif rule in ("succession", "transfer"):
                out = _run_succession(question, route, scope, pf_res)
                rules_res.update({k: v for k, v in out.items() if k != "trace"})
                rule_trace.extend(out.get("trace") or [])
            elif rule == "promotion":
                out = _run_promotion(question, route, scope)
                rules_res.update({k: v for k, v in out.items() if k != "trace"})
                rule_trace.extend(out.get("trace") or [])
            elif rule == "training":
                out = _run_training(question, route, scope)
                rules_res.update({k: v for k, v in out.items() if k != "trace"})
                rule_trace.extend(out.get("trace") or [])
            elif rule == "jd":
                out = _run_jd(question, route, scope, policy_res)
                rules_res.update({k: v for k, v in out.items() if k != "trace"})
                rule_trace.extend(out.get("trace") or [])
        except Exception as e:
            log.exception("rule %s failed", rule)
            rules_res[rule] = {"status": "ERROR", "reasons": [str(e)]}
        step(f"rule_{rule}", f"Rule engine: {rule}", "done")

    if route.get("hrms") and not rules_res.get("sanction"):
        step("hrms", "HRMS Text-to-SQL", "running")
        hrms_res = ask_hrms(question, scope=scope)
        evidence.append({"source": "hrms_sql", "data": hrms_res})
        step("hrms", "HRMS Text-to-SQL", "done")

    step("synth", "Synthesis", "running")
    synth_context = {
        "route": {k: route.get(k) for k in ("topic", "rules", "hrms", "pf", "policy")},
        "hrms": hrms_res or None,
        "pf": pf_res or None,
        "policy": policy_res[:6] if policy_res else None,
        "rules": rules_res or None,
    }
    answer = chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Answer using ONLY provided evidence. Cite HRMS / PF / Policy / Rules. "
                    "Do not invent figures."
                ),
            },
            {"role": "user", "content": f"Question: {question}\n\nEvidence:\n{synth_context}"},
        ],
        temperature=0.2,
        max_tokens=1600,
    )
    step("synth", "Synthesis", "done")
    return {
        "answer": answer,
        "evidence": evidence,
        "rule_trace": rule_trace,
        "topic": route.get("topic") or "general",
        "route": route,
        "hrms": hrms_res,
        "pf": pf_res,
        "policy": policy_res,
        "rules": rules_res,
        "sources_used": {
            "hrms": bool(route.get("hrms") or hrms_res),
            "pf": bool(route.get("pf") or pf_res),
            "policy": bool(route.get("policy") or policy_res),
            "rules": bool(route.get("rules") or rules_res),
        },
        "abstain_reasons": [],
    }


def run_pipeline(
    question: str,
    scope: AccessScope,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if scope.denied or scope.access_id is None:
        return {
            "answer": "Access denied. Sign in via HRMS launch (userid) before asking.",
            "evidence": [],
            "rule_trace": [],
            "topic": "auth",
            "route": {},
            "hrms": {},
            "pf": {},
            "policy": [],
            "rules": {},
            "sources_used": {},
            "abstain_reasons": ["auth_denied"],
        }

    if get_settings().agentic_explorer:
        return run_agent(question, scope, on_step=on_step)
    return _run_legacy_pipeline(question, scope, on_step=on_step)
