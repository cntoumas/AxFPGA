"""FPGA extensions on top of the unmodified upstream pyALSlib (PyPI ``pyalslib==1.3.1``).

These used to live in a local pyALSlib fork (commit 1155811, never published). They are
kept here so that AxFPGA depends only on the released pyALSlib:

- ``get_active_inputs`` / ``get_dropped_inputs_count``: support set of a LUT truth table.
- :class:`FpgaCatalogCache`: catalog query for approximate LUTs that *drop inputs*
  (fewer active inputs = smaller FPGA LUTs), used by the pyALS candidate generator.
- ``dead_cells`` / ``fpga_cost``: MFFC + support-weighted FPGA surrogate cost of an ALSGraph.
- ``remap_xilinx_7series``: Xilinx 7-Series re-mapping of a pyosys design.
- ``prep_design``: pyALSlib ``YosysHelper.prep_design`` with optional carry-chain protection.

Importing this module also installs a ``ys.SigMap`` fallback: pyosys 0.70 does not expose
``SigMap``, which upstream ``ALSGraph`` and ``YosysHelper`` call.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from pyalslib import ALSCatalogCache
from pyalslib.Utility import negate, string_to_nested_list_int
from pyosys import libyosys as ys


class _FallbackSigMap:
    """Minimal SigMap: follows module connections (lhs <- rhs) to canonical bits."""

    def __init__(self, module):
        self.module = module
        self.mapping = {}
        for conn in module.connections():
            for lhs, rhs in zip(conn[0].to_sigbit_vector(), conn[1].to_sigbit_vector()):
                self.mapping[lhs] = rhs

    def __call__(self, sig):
        out = ys.SigSpec()
        for bit in ys.SigSpec(sig).to_sigbit_vector():
            cur = bit
            while cur in self.mapping:
                cur = self.mapping[cur]
            out.append(cur)
        return out


def install_sigmap_fallback() -> bool:
    """Provide ``ys.SigMap`` if pyosys lacks it. Returns True if the fallback was installed."""
    if hasattr(ys, "SigMap"):
        return False
    ys.SigMap = _FallbackSigMap
    return True


install_sigmap_fallback()


# --------------------------------------------------------------------------- truth tables

def get_active_inputs(spec: str) -> List[int]:
    """Indices of the inputs a truth table depends on (cofactors T|x=0 and T|x=1 differ).

    ``spec`` is the truth table as a string of '0'/'1', entry k for input vector k.
    """
    n_bits = len(spec)
    active = []
    for j in range((n_bits - 1).bit_length()):
        step = 1 << j
        if any(spec[k] != spec[k + step] for k in range(n_bits) if not (k & step)):
            active.append(j)
    return active


def get_dropped_inputs_count(orig_spec: str, approx_spec: str) -> int:
    """Number of active inputs that ``approx_spec`` no longer depends on."""
    return max(0, len(get_active_inputs(orig_spec)) - len(get_active_inputs(approx_spec)))


# --------------------------------------------------------------------------- catalog

class FpgaCatalogCache(ALSCatalogCache):
    """ALSCatalogCache plus the input-dropping query used by the FPGA candidate generator."""

    def __init__(self, file_name: str):
        super().__init__(file_name)
        self.db_path = file_name

    def get_input_dropping_approx_luts(self, spec: str, max_dist: Optional[int] = None) -> List[Dict[str, Any]]:
        """Approximate LUTs for ``spec`` that depend on fewer inputs.

        Looks up ``spec`` (or, if absent, its negation) at distance > 0 (and <= ``max_dist``),
        keeps the entries that drop at least one active input, and orders them by most inputs
        dropped, then fewest gates, then smallest Hamming distance.
        """
        query = ("select synth_spec, S, P, out_p, out, depth, distance from luts "
                 "where spec = ? and distance > 0" + (" and distance <= ?" if max_dist else "") + " order by distance")
        connection = sqlite3.connect(self.db_path)
        try:
            cursor = connection.cursor()
            raw = []
            for key, negated in ((spec, False), (negate(spec), True)):
                cursor.execute(query, (key, max_dist) if max_dist else (key,))
                rows = cursor.fetchall()
                for r in rows:
                    synth = negate(r[0]) if negated else r[0]
                    out_p = 1 - r[3] if negated else r[3]
                    raw.append((synth, string_to_nested_list_int(r[1]), string_to_nested_list_int(r[2]), out_p, r[4], r[5], r[6]))
                if rows:
                    break
        finally:
            connection.close()

        orig_active = len(get_active_inputs(spec))
        candidates = []
        for synth, S, P, out_p, out, depth, distance in raw:
            active = len(get_active_inputs(synth))
            dropped = orig_active - active
            if dropped > 0:
                candidates.append({
                    "synth_spec": synth, "S": S, "P": P, "out_p": out_p, "out": out, "depth": depth,
                    "distance": distance, "dropped_inputs": dropped, "active_inputs": active,
                    "gates": len(S[0]) if S and len(S) > 0 else 0,
                })
        candidates.sort(key=lambda c: (-c["dropped_inputs"], c["gates"], c["distance"]))
        return candidates


# --------------------------------------------------------------------------- LUT graph cost

def dead_cells(graph, configuration: Optional[Dict[str, Any]] = None) -> list:
    """Cells of an ALSGraph whose outputs no primary output depends on any more (dead MFFCs),
    given the per-cell approximate specs in ``configuration`` ({cell name: {"axspec": ...}})."""
    cells = graph.get_cells()
    active_deps = {}
    for c in cells:
        spec = (configuration or {}).get(c["name"], {}).get("axspec", c["spec"])
        preds = c["in"]
        active_deps[c.index] = {preds[j] for j in get_active_inputs(spec) if j < len(preds)}
    live = {p for po in graph.get_po() for p in po["in"]}
    queue = list(live)
    while queue:
        cur = queue.pop(0)
        for p in active_deps.get(cur, ()):
            if p not in live:
                live.add(p)
                queue.append(p)
    return [c for c in cells if c.index not in live]


def fpga_cost(graph, configuration: Optional[Dict[str, Any]] = None,
              w_lut: float = 1.0, w_in: float = 0.25, w_mffc: float = 1.0) -> float:
    """MFFC + support-weighted FPGA surrogate cost of an ALSGraph:
    sum over live LUTs of (w_lut + w_in * |support|) minus w_mffc per dead cell."""
    dead = {c.index for c in dead_cells(graph, configuration)}
    cost = 0.0
    for c in graph.get_cells():
        if c.index in dead:
            cost -= w_mffc
        else:
            spec = (configuration or {}).get(c["name"], {}).get("axspec", c["spec"])
            cost += w_lut + w_in * len(get_active_inputs(spec))
    return cost


# --------------------------------------------------------------------------- Yosys helpers

def remap_xilinx_7series(design) -> None:
    """Re-map a pyosys design to Xilinx 7-Series primitives (no I/O pads)."""
    ys.run_pass("opt_clean -purge", design)
    ys.run_pass("opt -mux_undef -mux_bool -undriven -fine", design)
    try:
        ys.run_pass("synth_xilinx -family xc7 -noiopad -run begin:check", design)
    except Exception:
        ys.run_pass("synth_xilinx -family xc7 -noiopad", design)
    ys.run_pass("opt_clean -purge", design)


def protect_and_split_alus(helper, approx_nibble_bits: int = 4, min_width: int = 8, constant_ci: bool = False) -> int:
    """Black-box the MSB part of wide $alu cells (carry chains) before LUT mapping."""
    from axfpga.alu_splitter import AluSplitter
    splitter = AluSplitter(min_width=min_width, approx_nibble_bits=approx_nibble_bits, constant_ci=constant_ci)
    return splitter.protect_and_split(helper.design, top_module_name=helper.top_module, as_blackbox=True)


def prep_design(helper, cut_size: int, protect_carry_chains: bool = True, approx_nibble_bits: int = 4) -> None:
    """``YosysHelper.prep_design`` with optional carry-chain protection (``protect_and_split_alus``)."""
    if protect_carry_chains:
        protect_and_split_alus(helper, approx_nibble_bits=approx_nibble_bits)
    helper.prep_design(cut_size)
