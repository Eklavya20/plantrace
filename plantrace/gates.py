"""Engineering handoff is explicitly unavailable, regardless of manual review."""
from typing import Literal
from plantrace.models import EvidenceModel


class HandoffDecision(EvidenceModel):
    eligible: Literal[False] = False
    disposition: Literal["blocked"] = "blocked"
    policy: str = "engineering_handoff_not_implemented_v1"
    reasons: list[str]


def engineering_handoff_gate():
    return HandoffDecision(reasons=["Formal engineering handoff is not implemented.",
        "Reference declarations, numerical cross-checking, manual measurement review and provisional assembly are not proof of engineering safety."])
