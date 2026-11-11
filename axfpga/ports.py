"""Clock/reset port resolution shared by the verifier, trace extractor and pipeline."""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Sequence

logger = logging.getLogger("ports")

CLOCK_NAMES = ("clk", "clock", "clk_i")
RESET_NAMES_LOW = ("rst_n", "reset_n", "rstn", "rst_b", "reset_b")
RESET_NAMES_HIGH = ("rst", "reset", "rst_i")


def _names(inputs: Sequence[Mapping[str, Any]]):
    return [str(i["name"]).lstrip("\\") for i in inputs]


def _check_present(kind: str, name: str, names, source: str) -> str:
    name = str(name).lstrip("\\")
    if name not in names:
        raise ValueError(f"{kind} '{name}' ({source}) is not an input port of the design "
                         f"(inputs: {', '.join(names)})")
    return name


def resolve_clock_reset(inputs: Sequence[Mapping[str, Any]],
                        meta: Optional[Mapping[str, Any]] = None,
                        clock: Optional[str] = None,
                        reset: Optional[str] = None,
                        reset_active_low: Optional[bool] = None) -> Dict[str, Any]:
    """Return {"clk", "rst", "rst_active_low", "source", "clk_source", "rst_source"}."""
    meta = meta or {}
    names = _names(inputs)
    lower = {n.lower(): n for n in names}

    clk = rst = None
    clk_src = rst_src = "none"
    low: Optional[bool] = None

    if meta.get("clock"):
        clk, clk_src = _check_present("axc_clock", meta["clock"], names, "attribute"), "attribute"
    elif clock:
        clk, clk_src = _check_present("clock", clock, names, "argument"), "argument"
    else:
        for cand in CLOCK_NAMES:
            if cand in lower:
                clk, clk_src = lower[cand], "guess"
                break

    if meta.get("reset"):
        rst, rst_src = _check_present("axc_reset", meta["reset"], names, "attribute"), "attribute"
        low = bool(meta.get("reset_active_low", False))
    elif reset:
        rst, rst_src = _check_present("reset", reset, names, "argument"), "argument"
        low = reset_active_low
    else:
        for cand in RESET_NAMES_LOW + RESET_NAMES_HIGH:
            if cand in lower:
                rst, rst_src = lower[cand], "guess"
                low = cand in RESET_NAMES_LOW
                break

    if rst is not None and low is None:
        low = rst.lower() in RESET_NAMES_LOW or rst.lower().endswith("_n")
    if clk is not None and clk == rst:
        raise ValueError(f"'{clk}' is both clock and reset")

    for kind, name, src in (("clock", clk, clk_src), ("reset", rst, rst_src)):
        if src == "guess":
            logger.warning("%s '%s' guessed from its name; annotate the port with (* axc_%s *) "
                           "to make it explicit", kind, name, kind)

    first = clk_src if clk is not None else rst_src
    return {"clk": clk, "rst": rst, "rst_active_low": True if rst is None else bool(low),
            "source": first, "clk_source": clk_src, "rst_source": rst_src}
