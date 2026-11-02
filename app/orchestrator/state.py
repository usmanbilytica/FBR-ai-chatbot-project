from __future__ import annotations

from typing import Any, TypedDict



class GraphState(TypedDict, total=False):
    question: str
    scope: dict[str, Any]
    route: dict[str, Any]
    hrms: dict[str, Any]
    pf: dict[str, Any]
    policy: dict[str, Any]
    rules: dict[str, Any]
    answer: str
    evidence: list[dict[str, Any]]
    rule_trace: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    topic: str
    request_id: str
