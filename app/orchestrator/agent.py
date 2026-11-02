"""Agentic multi-source explorer — replaces hard source routing.

Each turn the LLM picks one tool (HRMS explore / SQL / PF / Policy / rules).
When finished (or max steps), evidence is synthesized into the final answer.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from app.auth.rbac import AccessScope
from app.core.config import get_settings
from app.orchestrator.tool_specs import TOOL_SPECS, tool_catalog_prompt
from app.orchestrator.tools import dispatch_tool
from app.services.llm import chat_completion, chat_json

log = logging.getLogger(__name__)

_CNIC_RE = re.compile(r"\b(\d{5}-\d{7}-\d|\d{13})\b")


def _extract_cnic(question: str) -> str | None:
    m = _CNIC_RE.search(question or "")
    return m.group(1) if m else None


def _parse_tool_call(raw: dict[str, Any] | str) -> tuple[str, dict[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("tool call must be object")
    # Allow {"tool":..., "args":...} or {"name":..., "arguments":...}
    name = raw.get("tool") or raw.get("name") or raw.get("action")
    args = raw.get("args") or raw.get("arguments") or raw.get("parameters") or {}
    if not name:
        # Sometimes model returns finish fields at top level
        if "answer_hint" in raw or raw.get("done") or raw.get("finished"):
            return "finish", {
                "answer_hint": raw.get("answer_hint") or raw.get("answer") or "",
                "topic": raw.get("topic") or "general",
            }
        raise ValueError("missing tool name")
    if not isinstance(args, dict):
        args = {}
    return str(name), args


def _known_tools() -> set[str]:
    return {t["name"] for t in TOOL_SPECS}


def run_agent(
    question: str,
    scope: AccessScope,
    *,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    max_steps = max(1, int(settings.agent_max_steps))

    def step(sid: str, label: str, status: str, detail: Any = None) -> None:
        if on_step:
            on_step({"id": sid, "label": label, "status": status, "detail": detail})

    cnic_hint = _extract_cnic(question)
    system = (
        "You are the FBR HR Decision Support multi-source explorer.\n"
        "Live sources: HRMS (entities+SQL), OCR Personal Files (full facets), Policy/JD corpus, rule engines.\n"
        "NO fixed router — explore like an analyst across ALL sources.\n\n"
        "HRMS: search_designations/locations/org_levels/grades/positions, describe_table, search_catalog, "
        "list_sanction_allocations, sanction_strength (grade|designation|position), run_hrms_sql, resolve_officer.\n"
        "PF: pf_resolve, pf_categories, pf_pages, pf_events, pf_segments, pf_files, pf_profile, "
        "pf_vector_search, pf_search.\n"
        "Policy: search_policies (filters), list_policy_docs, list_policy_doc_types, policy_corpus_stats, "
        "jd_authority, schedule_ii, list_schedule_ii.\n"
        "Rules: rule_promotion, rule_succession, rule_training, rule_jd — after facts are resolved.\n\n"
        "Prefer resolve-then-query. Do not invent numbers. Call finish when evidence suffices.\n\n"
        f"{tool_catalog_prompt()}"
    )
    if cnic_hint:
        system += f"\nDetected CNIC in question: {cnic_hint} — consider PF + officer resolve."

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Question: {question}\nAccessID={scope.access_id}. Begin exploration."},
    ]

    evidence: list[dict[str, Any]] = []
    tool_trace: list[dict[str, Any]] = []
    hrms_res: dict[str, Any] = {}
    pf_res: dict[str, Any] = {}
    policy_res: list[dict[str, Any]] = []
    rules_res: dict[str, Any] = {}
    answer_hint = ""
    topic = "general"
    sources_used = {"hrms": False, "pf": False, "policy": False, "rules": False}

    step("agent", "Agentic multi-source explore", "running", {"max_steps": max_steps})

    for i in range(max_steps):
        step(f"agent_step_{i+1}", f"Agent step {i+1}", "running")
        try:
            call = chat_json(messages, temperature=0.0)
            tool_name, tool_args = _parse_tool_call(call)
        except Exception as e:
            log.warning("agent tool-call parse failed at step %s: %s", i + 1, e)
            # Ask model to repair once
            messages.append(
                {
                    "role": "user",
                    "content": f"Previous response was invalid ({e}). Return ONLY JSON tool call.",
                }
            )
            try:
                call = chat_json(messages, temperature=0.0)
                tool_name, tool_args = _parse_tool_call(call)
            except Exception as e2:
                tool_trace.append({"step": i + 1, "error": str(e2)})
                step(f"agent_step_{i+1}", f"Agent step {i+1}", "done", {"error": str(e2)})
                break

        if tool_name not in _known_tools():
            messages.append(
                {
                    "role": "user",
                    "content": f"Unknown tool '{tool_name}'. Choose from: {sorted(_known_tools())}",
                }
            )
            step(f"agent_step_{i+1}", f"Agent step {i+1}", "done", {"unknown_tool": tool_name})
            continue

        # Seed CNIC into PF/officer tools when missing
        if cnic_hint and (
            tool_name.startswith("pf_")
            or tool_name in ("search_personal_files", "resolve_officer", "rule_promotion", "rule_training", "rule_jd")
        ):
            tool_args.setdefault("cnic", cnic_hint)

        result = dispatch_tool(tool_name, tool_args, scope=scope, question=question)
        tool_trace.append({"step": i + 1, "tool": tool_name, "args": tool_args, "ok": result.get("ok")})
        evidence.append({"source": f"tool:{tool_name}", "data": result})

        # Track aggregate buckets for API compatibility
        hrms_tools = {
            "search_catalog",
            "describe_table",
            "search_designations",
            "search_locations",
            "search_org_levels",
            "search_grades",
            "search_positions",
            "list_sanction_allocations",
            "run_hrms_sql",
            "sanction_strength",
            "resolve_officer",
        }
        pf_tools = {
            "pf_resolve",
            "pf_categories",
            "pf_pages",
            "pf_events",
            "pf_segments",
            "pf_files",
            "pf_profile",
            "pf_vector_search",
            "pf_search",
            "search_personal_files",
        }
        policy_tools = {
            "search_policies",
            "list_policy_docs",
            "list_policy_doc_types",
            "policy_corpus_stats",
            "jd_authority",
            "schedule_ii",
            "list_schedule_ii",
        }
        if tool_name in hrms_tools:
            sources_used["hrms"] = True
            if tool_name == "run_hrms_sql":
                hrms_res = result
            elif tool_name == "sanction_strength":
                rules_res["sanction"] = result
                sources_used["rules"] = True
                hrms_res = {**hrms_res, "sanction": result}
        elif tool_name in pf_tools:
            sources_used["pf"] = True
            pf_res = result
        elif tool_name in policy_tools:
            sources_used["policy"] = True
            if tool_name == "search_policies":
                policy_res = list(result.get("hits") or [])
            elif tool_name.startswith("rule_"):
                pass
            else:
                policy_res = policy_res or [result]
        if tool_name.startswith("rule_"):
            sources_used["rules"] = True
            rules_res[tool_name] = result
            if tool_name == "rule_jd" and result.get("policy_hits"):
                sources_used["policy"] = True
                policy_res = list(result.get("policy_hits") or policy_res)

        # Compact observation back to the model
        obs = json.dumps(result, default=str)
        if len(obs) > 6000:
            obs = obs[:6000] + "…[truncated]"
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps({"tool": tool_name, "args": tool_args}, default=str),
            }
        )
        messages.append({"role": "user", "content": f"Tool result:\n{obs}\n\nNext tool JSON (or finish)."})
        step(
            f"agent_step_{i+1}",
            f"Agent step {i+1}: {tool_name}",
            "done",
            {"ok": result.get("ok"), "tool": tool_name},
        )

        if tool_name == "finish" or result.get("finished"):
            answer_hint = str(result.get("answer_hint") or answer_hint)
            topic = str(result.get("topic") or topic)
            break
    else:
        answer_hint = answer_hint or "Max exploration steps reached — synthesize from evidence gathered."

    step("agent", "Agentic multi-source explore", "done", {"steps": len(tool_trace)})

    # Final synthesis — compact evidence so long policy/PF payloads don't blank the LLM
    step("synth", "Synthesis", "running")

    def _compact(obj: Any, depth: int = 0) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if k in {"extracted_text", "text", "summary", "schema_pack"} and isinstance(v, str):
                    out[k] = v[:900] + ("…" if len(v) > 900 else "")
                elif k in {"pages", "events", "hits", "rows", "allocations", "positions"} and isinstance(v, list):
                    out[k] = [_compact(x, depth + 1) for x in v[:8]]
                else:
                    out[k] = _compact(v, depth + 1)
            return out
        if isinstance(obj, list):
            return [_compact(x, depth + 1) for x in obj[:12]]
        return obj

    synth_context = {
        "mode": "agentic_explorer",
        "topic": topic,
        "answer_hint": answer_hint,
        "sources_used": sources_used,
        "tool_trace": [{"step": t.get("step"), "tool": t.get("tool"), "ok": t.get("ok")} for t in tool_trace],
        "evidence": _compact(evidence[-8:]),
    }
    try:
        answer = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the FBR HR Decision Support assistant. "
                        "Answer using ONLY the exploration evidence (HRMS / Personal Files / Policy / Rules tools). "
                        "Cite sources. Prefer concrete numbers from sanction_strength or SQL rows. "
                        "If evidence is incomplete, say what is missing. Never invent figures. "
                        "Always produce a non-empty answer. Be concise and operational."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\nExploration evidence JSON:\n"
                        f"{json.dumps(synth_context, default=str)[:12000]}"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=1600,
        )
    except Exception as e:
        log.warning("synth chat_completion failed: %s", e)
        answer = ""

    if not (answer or "").strip():
        # Prefer explorer finish hint; else stitch compact policy/PF snippets
        if (answer_hint or "").strip():
            answer = answer_hint.strip()
            log.warning("synth empty — using finish answer_hint")
        else:
            snippets: list[str] = []
            for ev in evidence[-6:]:
                data = ev.get("data") if isinstance(ev, dict) else None
                if not isinstance(data, dict):
                    continue
                for hit in (data.get("hits") or [])[:3]:
                    if isinstance(hit, dict) and hit.get("text"):
                        snippets.append(
                            f"- {(hit.get('doc_name') or 'policy')}: {str(hit.get('text'))[:400]}"
                        )
                if data.get("answer_hint"):
                    snippets.append(str(data["answer_hint"]))
            answer = (
                "Exploration completed but final synthesis was empty. Evidence excerpts:\n"
                + ("\n".join(snippets) if snippets else json.dumps(sources_used))
            )
            log.warning("synth empty — fallback excerpt answer")

    step("synth", "Synthesis", "done", {"sources_used": sources_used, "answer_len": len(answer or "")})

    abstain_reasons: list[str] = []
    if not any(sources_used.values()):
        abstain_reasons.append("no_tools_succeeded")
    if hrms_res.get("error") and not hrms_res.get("ok"):
        abstain_reasons.append(f"hrms:{hrms_res.get('error')}")
    if pf_res.get("denied"):
        abstain_reasons.append("pf:scope_denied")

    log.info(
        "agent_complete topic=%s steps=%s sources=%s",
        topic,
        len(tool_trace),
        sources_used,
    )

    rule_trace = []
    for k, v in rules_res.items():
        if isinstance(v, dict):
            rule_trace.append({"rule": k, **{kk: v.get(kk) for kk in ("status", "ok", "reasons", "sanctioned", "vacant") if kk in v}})

    return {
        "answer": answer,
        "evidence": evidence,
        "rule_trace": rule_trace,
        "topic": topic,
        "route": {"mode": "agentic", "topic": topic, "steps": len(tool_trace)},
        "hrms": hrms_res,
        "pf": pf_res,
        "policy": policy_res,
        "rules": rules_res,
        "sources_used": sources_used,
        "abstain_reasons": abstain_reasons,
        "tool_trace": tool_trace,
        "answer_hint": answer_hint,
    }
