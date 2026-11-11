"""Proof outcomes and certification tiers."""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Optional


class ProofStatus(str, Enum):
    PROVEN = "PROVEN"                # holds for every reachable state / every input
    BOUNDED = "BOUNDED"              # holds for the first N cycles from reset (cycles in the result)
    TIMEOUT = "TIMEOUT"
    INCONCLUSIVE = "INCONCLUSIVE"    # the solver ended without a proof or a counterexample
    FALSIFIED = "FALSIFIED"          # counterexample found
    ERROR = "ERROR"                  # the check could not be set up or run
    NA = "N/A"                       # not run

    @classmethod
    def parse(cls, value: object) -> "ProofStatus":
        """Status from a result string such as "PROVEN", "BOUNDED(16)" or "N/A (exact design kept)"."""
        text = str(value.value if isinstance(value, ProofStatus) else value).strip()
        for status in cls:
            if text == status.value or text.startswith(status.value + "(") or text.startswith(status.value + " "):
                return status
        return cls.ERROR


class Certification(str, Enum):
    CERTIFIED = "certified"
    BOUNDED = "bounded-certified"
    UNCERTIFIED = "uncertified"


def certify(statuses: Iterable[object], exact_design: bool = False) -> Certification:
    """Certification tier of a reported design from the status of each applicable proof."""
    if exact_design:
        return Certification.CERTIFIED
    parsed = [ProofStatus.parse(s) for s in statuses]
    if not parsed or any(s not in (ProofStatus.PROVEN, ProofStatus.BOUNDED) for s in parsed):
        return Certification.UNCERTIFIED
    if all(s is ProofStatus.PROVEN for s in parsed):
        return Certification.CERTIFIED
    return Certification.BOUNDED


def status_text(status: ProofStatus, cycles: Optional[int] = None) -> str:
    """Display form: ``BOUNDED(16)`` for bounded results, the plain value otherwise."""
    return f"{status.value}({cycles})" if status is ProofStatus.BOUNDED and cycles is not None else status.value


def certification_record(formal_control: object, formal_wce: object, exact_design: bool = False) -> dict:
    """The ``certification`` field of a result: tier, per-proof status and, when not certified, why."""
    ctrl, wce = ProofStatus.parse(formal_control), ProofStatus.parse(formal_wce)
    tier = certify([formal_control, formal_wce], exact_design=exact_design)
    reason = ""
    if exact_design:
        reason = "exact design kept: no approximation applied"
    elif tier is not Certification.CERTIFIED:
        bad = [f"{n}={s.value}" for n, s in (("control_equivalence", ctrl), ("wce_bound", wce))
               if s is not ProofStatus.PROVEN]
        reason = ("; ".join(bad) + (" (holds for a bounded number of cycles only)" if tier is Certification.BOUNDED else
                                    " (no guarantee)"))
    return {"tier": tier.value, "proofs": {"control_equivalence": ctrl.value, "wce_bound": wce.value}, "reason": reason}


def result_tier(result: dict) -> Certification:
    """Tier of a result record; records written before the field existed are derived from their formal statuses."""
    cert = result.get("certification")
    if isinstance(cert, dict) and "tier" in cert:
        return Certification(cert["tier"])
    f = result.get("formal", {})
    return certify([f.get("control_equivalence"), f.get("wce_bound")], exact_design=bool(result.get("fell_back")))
