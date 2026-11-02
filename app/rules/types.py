from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    PASS = "PASS"
    FAIL = "HARD_FAIL"
    BLOCK = "BLOCK"
    EXCLUDE = "EXCLUDE"
    WARNING = "WARNING"
    NOTE = "NOTE"
    MISSING = "MISSING_INPUTS"
    SOFT_NO = "SOFT_NO"


@dataclass
class RuleResult:
    status: Severity
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reasons": self.reasons,
            "evidence": self.evidence,
            "citations": self.citations,
            "data": self.data,
        }
