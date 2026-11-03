"""Proof outcomes and certification tiers.

Every formal check reports one :class:`ProofStatus`. A reported design is

- **certified** when every applicable proof is PROVEN;
- **bounded-certified** when every proof is PROVEN or BOUNDED (holds for the first N cycles
  from reset, not proven forever) and at least one is BOUNDED;
- **uncertified** otherwise: any TIMEOUT, INCONCLUSIVE, ERROR, N/A or FALSIFIED.

N/A counts as uncertified on purpose: a proof that applies to the design but was not run
(e.g. a WCE proof for design-level PPG pruning) gives no guarantee. The exact design itself
(no approximation applied) is certified trivially.
"""

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
