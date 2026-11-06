"""Per-site RTL operators on a word-level cone netlist (pyosys), for the design-space exploration.

Runs in a subprocess (``python -m axfpga.explore.ops spec.json result.json``): libyosys keeps global state.

Site kinds (ids are stable across runs):
  add@ / sub@ / mul@<file:line.col-line.col>   arithmetic cells (``axfpga.operators.core._site_cells``)
  wire@<name>                                  named multi-bit wire of the cone (input ports included)
  out@<name>                                   the relaxed output bus

Operators (a gene is ``{"op": ..., "k": int, "comp": "zero"|"mid"}``; k = 0 leaves the site untouched):
  trunc   arithmetic cell: k operand LSBs tied to 0, freed output bits driven by the correction constant
  wire    named wire: every read of its k LSBs sees the correction constant instead
  out     relaxed output bus: its k LSBs are driven by the correction constant
  loa     adder: lower-part OR adder, k low bits OR-ed, carry into the upper part = a[k-1] & b[k-1]
  nibble  adder: the k low 4-bit segments are added without carries between them (segmented carry chain)
  ppg     multiplier: partial products a_i * b_j with i + j < k removed (Baugh-Wooley array for signed operands)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from axfpga.operators import core

OPS = ("trunc", "wire", "out", "loa", "nibble", "ppg")
COMPS = ("zero", "mid")
MAX_WIRE_SITES = 16


def _ys():
    from pyosys import libyosys as ys
    return ys


def _id(name: str):
    return _ys().IdString(name)


def _param_int(cell, name: str, default: int = 0) -> int:
    p = cell.parameters.get(_id(name))
    if p is None:
        return default
    try:
        return int(p.as_int())
    except Exception:
        return default


def _extended(cell, port: str, width: int):
    sig = cell.getPort(_id("\\" + port))
    signed = bool(_param_int(cell, f"\\{port}_SIGNED"))
    s = _ys().SigSpec(sig)
    s.extend_u0(width, signed)
    return s


def _wire_driven_bits(module) -> set:
    """(wire name, offset) of every bit driven by a cell output or a connection."""
    out = set()
    for _, cell in module.cells_.items():
        for pid, sig in cell.connections().items():
            if cell.output(pid):
                for b in sig.bits():
                    if b.wire is not None:
                        out.add((b.wire.name.str(), b.offset))
    for lhs, _ in module.connections():
        for b in lhs.bits():
            if b.wire is not None:
                out.add((b.wire.name.str(), b.offset))
    return out


def list_sites(module, out_wire: Optional[str] = None) -> List[Dict[str, Any]]:
    """Arithmetic, wire and output sites of the cone."""
    sites = []
    for s in core.list_sites(module, out_wire):
        s = dict(s)
        if s["kind"] == "out":
            s["ops"] = ["out"]
        else:
            ops = ["trunc"]
            if s["kind"] == "add":
                ops += ["loa", "nibble"]
            if s["kind"] == "mul" and not (s["a"]["const"] or s["b"]["const"]):
                ops.append("ppg")
            s["ops"] = ops
            var = [o for o in (s["a"], s["b"]) if not o["const"]]
            s["max_loa"] = s["max_k"]
            s["max_nibble"] = max(0, (min(o["eff_width"] for o in var) - 1) // 4) if var else 0
            s["max_ppg"] = (s["a"]["eff_width"] + s["b"]["eff_width"] - 2) if s["kind"] == "mul" else 0
        sites.append(s)
    out_name = (out_wire or "").lstrip("\\")
    wires = []
    for name, w in module.wires_.items():
        n = name.str()
        if n.startswith("$") or w.width < 2 or n.lstrip("\\") == out_name or w.port_output:
            continue
        # effective width: drop constant MSBs is not known per wire; use the declared width
        fan = 0
        for _, cell in module.cells_.items():
            for pid, sig in cell.connections().items():
                if not cell.output(pid) and any(b.wire is not None and b.wire.name == name for b in sig.bits()):
                    fan += 1
        if fan == 0:
            continue
        wires.append({"id": f"wire@{n.lstrip(chr(92))}", "kind": "wire", "y_width": w.width, "a": None, "b": None,
                      "max_k": w.width - 1, "weight": w.width * fan, "fanout": fan, "port": bool(w.port_input),
                      "ops": ["wire"]})
    wires.sort(key=lambda s: (-s["weight"], s["id"]))
    return sites + wires[:MAX_WIRE_SITES]


def _subst_reads(module, wire, k: int, value: int) -> None:
    """Every read of wire[0:k] (cell inputs, connection right-hand sides) sees the constant ``value`` instead."""
    ys = _ys()
    const_bits = [ys.State.S1 if (value >> i) & 1 else ys.State.S0 for i in range(k)]

    def fix(sig):
        changed = False
        bits = []
        for b in sig.bits():
            if b.wire is not None and b.wire.name == wire.name and b.offset < k:
                bits.append(ys.SigBit(const_bits[b.offset]))
                changed = True
            else:
                bits.append(b)
        return ys.SigSpec(bits) if changed else None

    for _, cell in module.cells_.items():
        for pid, sig in list(cell.connections().items()):
            if not cell.output(pid):
                new = fix(sig)
                if new is not None:
                    cell.setPort(pid, new)
    conns = []
    for lhs, rhs in module.connections():
        new = fix(rhs)
        conns.append((lhs, new if new is not None else rhs))
    module.new_connections(conns)


def _apply_wire(module, name: str, k: int, comp: str) -> None:
    ys = _ys()
    w = module.wire(_id("\\" + name))
    if w is None:
        raise KeyError(f"wire {name} not in the cone")
    k = min(k, w.width - 1)
    if k <= 0:
        return
    if w.port_output:
        core._apply_out(module, name, k, comp)
        return
    _subst_reads(module, w, k, core._comp_value(comp, k))


def _uniq(module, base: str):
    return module.uniquify(_id("\\" + base))


def _apply_loa(module, cell, k: int) -> None:
    ys = _ys()
    y = cell.getPort(_id("\\Y"))
    yw = y.size()
    k = min(k, yw - 1)
    if k <= 0:
        return
    a, b = _extended(cell, "A", yw), _extended(cell, "B", yw)
    lo = module.addWire(_uniq(module, "axc_loa_lo"), k)
    module.addOr(_uniq(module, "axc_loa_or"), a.extract(0, k), b.extract(0, k), ys.SigSpec(lo))
    hi_w = yw - k
    cin = module.addWire(_uniq(module, "axc_loa_c"), 1)
    module.addAnd(_uniq(module, "axc_loa_and"), a.extract(k - 1, 1), b.extract(k - 1, 1), ys.SigSpec(cin))
    s1 = module.addWire(_uniq(module, "axc_loa_s1"), hi_w)
    module.addAdd(_uniq(module, "axc_loa_add"), a.extract(k, hi_w), b.extract(k, hi_w), ys.SigSpec(s1))
    cin_ext = ys.SigSpec(cin)
    cin_ext.extend_u0(hi_w, False)
    hi = module.addWire(_uniq(module, "axc_loa_hi"), hi_w)
    module.addAdd(_uniq(module, "axc_loa_addc"), ys.SigSpec(s1), cin_ext, ys.SigSpec(hi))
    new_y = ys.SigSpec(lo)
    new_y.append(ys.SigSpec(hi))
    module.remove(cell)
    module.connect(y, new_y)


def _apply_nibble(module, cell, k: int) -> None:
    ys = _ys()
    y = cell.getPort(_id("\\Y"))
    yw = y.size()
    k = min(k, (yw - 1) // 4)
    if k <= 0:
        return
    a, b = _extended(cell, "A", yw), _extended(cell, "B", yw)
    parts = []
    for s in range(k):
        seg = module.addWire(_uniq(module, "axc_nib"), 4)
        module.addAdd(_uniq(module, "axc_nib_add"), a.extract(4 * s, 4), b.extract(4 * s, 4), ys.SigSpec(seg))
        parts.append(ys.SigSpec(seg))
    hi_w = yw - 4 * k
    hi = module.addWire(_uniq(module, "axc_nib_hi"), hi_w)
    module.addAdd(_uniq(module, "axc_nib_add_hi"), a.extract(4 * k, hi_w), b.extract(4 * k, hi_w), ys.SigSpec(hi))
    new_y = ys.SigSpec()
    for p in parts:
        new_y.append(p)
    new_y.append(ys.SigSpec(hi))
    module.remove(cell)
    module.connect(y, new_y)


def _apply_ppg(module, cell, k: int) -> None:
    """Replace a multiplier by its partial-product array without the products of weight below 2^k."""
    ys = _ys()
    y = cell.getPort(_id("\\Y"))
    yw = y.size()
    if k <= 0:
        return
    sa, sb = bool(_param_int(cell, "\\A_SIGNED")), bool(_param_int(cell, "\\B_SIGNED"))
    a_sig, b_sig = cell.getPort(_id("\\A")), cell.getPort(_id("\\B"))
    n = min(core._effective_width(a_sig), yw)
    m = min(core._effective_width(b_sig), yw)
    signed = sa and sb
    if (sa or sb) and not signed:            # mixed signedness: treat both as signed on one extra bit
        n, m, signed = min(n + 1, yw), min(m + 1, yw), True
    a = ys.SigSpec(a_sig)
    a.extend_u0(max(n, a_sig.size()), sa)
    b = ys.SigSpec(b_sig)
    b.extend_u0(max(m, b_sig.size()), sb)
    a_bits, b_bits = list(a.bits())[:n], list(b.bits())[:m]
    zero = ys.SigBit(ys.State.S0)

    def row(bits_x, bit_y, shift, keep) -> Optional[Any]:
        """(x & {bit_y}) << shift on yw bits, the terms with keep(i) False removed; None when empty."""
        xs = [bits_x[i] if keep(i) else zero for i in range(len(bits_x))]
        if all(x == zero for x in xs) or shift >= yw:
            return None
        width = min(len(xs), yw - shift)
        xs = xs[:width]
        t = module.addWire(_uniq(module, "axc_pp"), width)
        module.addAnd(_uniq(module, "axc_pp_and"), ys.SigSpec(xs), ys.SigSpec(bit_y, width), ys.SigSpec(t))
        out = ys.SigSpec(ys.Const(0, shift)) if shift else ys.SigSpec()
        out.append(ys.SigSpec(t))
        if out.size() < yw:
            out.append(ys.SigSpec(ys.Const(0, yw - out.size())))
        return out

    pos: List[Any] = []
    neg: List[Any] = []
    if not signed:
        for j in range(m):
            r = row(a_bits, b_bits[j], j, lambda i, j=j: i + j >= k)
            if r is not None:
                pos.append(r)
    else:
        # Baugh-Wooley: sum_{i<n-1,j<m-1} a_i b_j 2^(i+j) + a_{n-1} b_{m-1} 2^(n+m-2)
        #               - sum_{j<m-1} a_{n-1} b_j 2^(n-1+j) - sum_{i<n-1} a_i b_{m-1} 2^(i+m-1)
        for j in range(m - 1):
            r = row(a_bits[:n - 1], b_bits[j], j, lambda i, j=j: i + j >= k)
            if r is not None:
                pos.append(r)
        if n + m - 2 >= k:
            r = row([a_bits[n - 1]], b_bits[m - 1], n + m - 2, lambda i: True)
            if r is not None:
                pos.append(r)
        r = row(b_bits[:m - 1], a_bits[n - 1], n - 1, lambda j: n - 1 + j >= k)
        if r is not None:
            neg.append(r)
        r = row(a_bits[:n - 1], b_bits[m - 1], m - 1, lambda i: i + m - 1 >= k)
        if r is not None:
            neg.append(r)

    def total(terms: List[Any]) -> Any:
        if not terms:
            return ys.SigSpec(ys.Const(0, yw))
        acc = terms[0]
        for t in terms[1:]:
            w = module.addWire(_uniq(module, "axc_pp_sum"), yw)
            module.addAdd(_uniq(module, "axc_pp_add"), acc, t, ys.SigSpec(w))
            acc = ys.SigSpec(w)
        return acc

    p, q = total(pos), total(neg)
    module.remove(cell)
    if neg:
        module.addSub(_uniq(module, "axc_pp_sub"), p, q, y)
    else:
        module.connect(y, p)


def apply_genome(module, genome: Dict[str, Dict[str, Any]], out_wire: Optional[str] = None) -> List[str]:
    """Apply a genome in place (genes with k = 0 are skipped); returns the applied site ids."""
    cells = {sid: (cell, kind) for sid, cell, kind in core._site_cells(module)}
    done = []
    for sid in sorted(genome):
        gene = genome[sid]
        op, k, comp = gene.get("op", "trunc"), int(gene.get("k", 0)), gene.get("comp", "zero")
        if k <= 0:
            continue
        if op not in OPS:
            raise ValueError(f"unknown operator {op!r} for site {sid}")
        if op == "out":
            if out_wire is None:
                raise ValueError("output site without an output wire")
            core._apply_out(module, out_wire, k, comp)
        elif op == "wire":
            _apply_wire(module, sid.split("@", 1)[1], k, comp)
        else:
            if sid not in cells:
                raise KeyError(f"site {sid} not found in the cone")
            cell, kind = cells[sid]
            if op == "trunc":
                core._apply_trunc(module, cell, kind, k, comp)
            elif op == "loa":
                _apply_loa(module, cell, k)
            elif op == "nibble":
                _apply_nibble(module, cell, k)
            elif op == "ppg":
                _apply_ppg(module, cell, k)
        done.append(sid)
    return done


def run_batch(spec: Dict[str, Any]) -> Dict[str, Any]:
    """spec: {"cone", "module"?, "out_wire"?, "list_sites"?, "genomes": {name: genome}, "out_dir"}."""
    result: Dict[str, Any] = {"sites": [], "written": {}, "errors": {}}
    if spec.get("list_sites"):
        _, m = core.load(spec["cone"], spec.get("module"))
        result["sites"] = list_sites(m, spec.get("out_wire"))
    for name, genome in spec.get("genomes", {}).items():
        try:
            d, m = core.load(spec["cone"], spec.get("module"))
            apply_genome(m, genome, spec.get("out_wire"))
            path = os.path.join(spec["out_dir"], f"{name}.v")
            core.write(d, path)
            result["written"][name] = path
        except Exception as exc:
            result["errors"][name] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: Optional[List[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    spec = json.loads(open(argv[0]).read())
    res = run_batch(spec)
    open(argv[1], "w").write(json.dumps(res))


if __name__ == "__main__":
    main()
