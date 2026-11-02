#!/usr/bin/env python
"""Golden prompt smoke — routes intents without requiring full LLM success."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.orchestrator.graph import route_intent

PROMPTS = [
    "What is the sanctioned strength by grade for BS-18?",
    "Show position vacancy for BS-17 seats",
    "Who can be permanently transferred to a vacant chair?",
    "OPS nomination rules for BPS-19",
    "Can IRS officer hold PCS seat?",
    "Promotion from BS-18 to BS-19 MCMC requirement",
    "Training nomination LMS course",
    "Personal file E&D inquiry against officer",
    "What do APT Rules say about seniority?",
    "Designation sanctioned strength",
]


def main() -> None:
    ok = 0
    for p in PROMPTS:
        r = route_intent(p)
        print(p, "->", {k: r.get(k) for k in ("topic", "hrms", "pf", "policy", "rules")})
        if r.get("hrms") or r.get("pf") or r.get("policy") or r.get("rules"):
            ok += 1
    print(f"routed {ok}/{len(PROMPTS)}")


if __name__ == "__main__":
    main()
