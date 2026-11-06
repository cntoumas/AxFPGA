"""RTL-level approximation operators on a word-level cone netlist (pyosys).

Used inside a subprocess (``python -m axfpga.operators.core``), never in the pipeline process: libyosys keeps global
state, and running the operators out of process keeps the other in-process Yosys steps (and so their cache keys)
independent of how many candidates were generated.

Operators (a *gene* per *site*):

``trunc``  Operand LSB truncation with a constant correction, on an arithmetic cell (``$add``, ``$sub``, ``$mul``).
           The k low bits of every variable operand are tied to 0. The output bits that are then known to be zero
           (``z`` of them) are driven by a constant ``c`` instead of the cell: ``c`` is 0 (plain truncation),
           2^(z-1) (``mid``: half of the dropped range, the usual rounding offset) or 2^z - 1 (``max``). The correction
           costs nothing: it only replaces constant-zero bits by other constants.
``out``    Output truncation: the k low bits of the relaxed output bus are tied to the constant ``c`` (same menu with
           z = k), on any cone, whatever its logic.

A gene is ``{"k": int, "comp": "zero" | "mid" | "max"}``. A genome is ``{site_id: gene}``.
Site ids are stable across runs: ``<kind>@<file>:<line.col-line.col>`` from the cell's ``src`` attribute, with ``#n``
for duplicates; the output site is ``out@<wire>``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

ARITH = {"$add": "add", "$sub": "sub", "$mul": "mul"}
COMPS = ("zero", "mid", "max")


def _ys():
    from pyosys import libyosys as ys
    return ys


def load(cone_path: str, module: Optional[str] = None):
    """Read a cone netlist; return (design, module). Quiet: Yosys log goes to nowhere."""
    ys = _ys()
    d = ys.Design()
    ys.run_pass(f"read_verilog -sv {cone_path}; hierarchy -check; proc; opt_clean", d)
    m = d.module(ys.IdString("\\" + module)) if module else d.top_module()
    return d, m


def _bits(sig) -> List[Any]:
    return list(sig.bits())


def _trailing_zero_bits(sig) -> int:
    """Number of constant-0 bits at the LSB end of a SigSpec."""
    ys = _ys()
    n = 0
    for b in _bits(sig):
        if b.wire is None and b.data == ys.State.S0:
            n += 1
        else:
            break
    return n


def _is_const(sig) -> bool:
    return all(b.wire is None for b in _bits(sig))


def _src_id(cell, kind: str) -> Optional[str]:
    ys = _ys()
    attr = cell.attributes.get(ys.IdString("\\src"))
    if attr is None:
        return None
    s = attr.decode_string() if hasattr(attr, "decode_string") else str(attr)
    first = s.split("|")[0]
    m = re.match(r"^(?:.*/)?([^/:]+):(\d+\.\d+-\d+\.\d+)$", first)
    return f"{kind}@{m.group(1)}:{m.group(2)}" if m else None


def _site_cells(module) -> List[Tuple[str, Any, str]]:
    """(site id, cell, kind) of every arithmetic cell, ids unique and deterministic."""
    raw = []
    for name, cell in module.cells_.items():
        t = cell.type.str()
        if t in ARITH:
            raw.append((name.str(), cell, ARITH[t]))
    raw.sort(key=lambda r: r[0])
    seen: Dict[str, int] = {}
    out = []
    for i, (name, cell, kind) in enumerate(raw):
        sid = _src_id(cell, kind) or f"{kind}@cell{i}"
        seen[sid] = seen.get(sid, 0) + 1
        if seen[sid] > 1:
            sid = f"{sid}#{seen[sid]}"
        out.append((sid, cell, kind))
    return out


def _effective_width(sig) -> int:
    """Width without the redundant top bits (sign or zero extension: a bit equal to the one below it)."""
    bits = _bits(sig)
    n = len(bits)
    while n > 1 and bits[n - 1] == bits[n - 2]:
        n -= 1
    if n > 1 and all(b.wire is None for b in bits[n - 1:]) and bits[n - 1].data == _ys().State.S0 and bits[n - 1] != bits[n - 2]:
        n -= 1                       # a constant-0 MSB above a variable bit: zero extension of an unsigned operand
    return n


def _operand_info(cell, port: str) -> Dict[str, Any]:
    sig = cell.getPort(_ys().IdString(port))
    return {"width": sig.size(), "eff_width": _effective_width(sig), "const": _is_const(sig),
            "tz": _trailing_zero_bits(sig)}


def list_sites(module, out_wire: Optional[str] = None) -> List[Dict[str, Any]]:
    """Sites of a cone: arithmetic cells and (if ``out_wire`` is given) the relaxed output bus."""
    ys = _ys()
    sites = []
    for sid, cell, kind in _site_cells(module):
        a, b = _operand_info(cell, "\\A"), _operand_info(cell, "\\B")
        y = cell.getPort(ys.IdString("\\Y")).size()
        var = [o for o in (a, b) if not o["const"]]
        cap = max(0, min(o["eff_width"] for o in var) - 1) if var else 0
        sites.append({"id": sid, "kind": kind, "y_width": y, "a": a, "b": b, "max_k": cap,
                      "weight": y * max(o["width"] for o in (a, b))})
    if out_wire:
        w = module.wire(ys.IdString("\\" + out_wire.lstrip("\\")))
        if w is not None:
            sites.append({"id": f"out@{out_wire.lstrip(chr(92))}", "kind": "out", "y_width": w.width,
                          "a": None, "b": None, "max_k": max(0, w.width - 1), "weight": w.width})
    return sites


def _const_sig(value: int, width: int):
    ys = _ys()
    return ys.SigSpec(ys.Const(value, width))


def _zero_low(sig, k: int):
    """SigSpec with the k low bits tied to 0."""
    ys = _ys()
    k = min(k, sig.size())
    out = _const_sig(0, k) if k else ys.SigSpec()
    if sig.size() - k:
        out.append(sig.extract(k, sig.size() - k))
    return out


def _comp_value(comp: str, z: int) -> int:
    if z <= 0 or comp == "zero":
        return 0
    if comp == "mid":
        return 1 << (z - 1)
    if comp == "max":
        return (1 << z) - 1
    raise ValueError(f"unknown compensation '{comp}'")


def _apply_trunc(module, cell, kind: str, k: int, comp: str) -> None:
    ys = _ys()
    a_id, b_id, y_id = ys.IdString("\\A"), ys.IdString("\\B"), ys.IdString("\\Y")
    a, b = cell.getPort(a_id), cell.getPort(b_id)
    tz = []
    for pid, sig in ((a_id, a), (b_id, b)):
        if _is_const(sig):
            tz.append(_trailing_zero_bits(sig))
        else:
            kk = min(k, max(0, _effective_width(sig) - 1))
            cell.setPort(pid, _zero_low(sig, kk))
            tz.append(max(kk, _trailing_zero_bits(sig)))
    y = cell.getPort(y_id)
    z = min(tz) if kind in ("add", "sub") else tz[0] + tz[1]
    z = min(z, y.size())
    c = _comp_value(comp, z)
    if z <= 0:
        return
    tmp = module.addWire(module.uniquify(ys.IdString("\\axc_tmp")), z)
    new_y = ys.SigSpec(tmp)
    if y.size() - z:
        new_y.append(y.extract(z, y.size() - z))
    cell.setPort(y_id, new_y)
    module.connect(y.extract(0, z), _const_sig(c, z))


def _apply_out(module, out_wire: str, k: int, comp: str) -> None:
    ys = _ys()
    w = module.wire(ys.IdString("\\" + out_wire.lstrip("\\")))
    k = min(k, w.width - 1)
    if k <= 0:
        return
    pre = module.addWire(module.uniquify(ys.IdString("\\axc_pre")), w.width)

    def retarget(sig):
        changed = False
        bits = []
        for b in _bits(sig):
            if b.wire is not None and b.wire.name == w.name:
                bits.append(ys.SigBit(pre, b.offset))
                changed = True
            else:
                bits.append(b)
        return ys.SigSpec(bits) if changed else None

    for _, cell in module.cells_.items():
        for pid, sig in list(cell.connections().items()):
            if cell.output(pid):
                new = retarget(sig)
                if new is not None:
                    cell.setPort(pid, new)
    conns = []
    for lhs, rhs in module.connections():
        new = retarget(lhs)
        conns.append((new if new is not None else lhs, rhs))
    module.new_connections(conns)
    final = _const_sig(_comp_value(comp, k), k)
    final.append(ys.SigSpec(pre).extract(k, w.width - k))
    module.connect(ys.SigSpec(w), final)


def apply_genome(module, genome: Dict[str, Dict[str, Any]], out_wire: Optional[str] = None) -> List[str]:
    """Apply a genome in place; returns the ids of the genes that were applied."""
    cells = {sid: (cell, kind) for sid, cell, kind in _site_cells(module)}
    done = []
    for sid in sorted(genome):
        gene = genome[sid]
        k, comp = int(gene["k"]), gene.get("comp", "zero")
        if comp not in COMPS:
            raise ValueError(f"unknown compensation '{comp}' for site {sid}")
        if sid.startswith("out@"):
            if out_wire is None:
                raise ValueError("output site without an output wire")
            _apply_out(module, out_wire, k, comp)
            done.append(sid)
        elif sid in cells:
            cell, kind = cells[sid]
            _apply_trunc(module, cell, kind, k, comp)
            done.append(sid)
        else:
            raise KeyError(f"site {sid} not found in the cone")
    return done


def write(design, path: str) -> None:
    ys = _ys()
    ys.run_pass("opt_clean -purge; clean", design)
    ys.run_pass(f"write_verilog -noattr {path}", design)


def run_batch(spec: Dict[str, Any]) -> Dict[str, Any]:
    """spec: {"cone", "out_wire", "module"?, "list_sites"?, "genomes": {name: genome}, "out_dir"}.
    Returns {"sites": [...], "written": {name: path}, "errors": {name: message}}."""
    result: Dict[str, Any] = {"sites": [], "written": {}, "errors": {}}
    if spec.get("list_sites"):
        _, m = load(spec["cone"], spec.get("module"))
        result["sites"] = list_sites(m, spec.get("out_wire"))
    for name, genome in spec.get("genomes", {}).items():
        try:
            d, m = load(spec["cone"], spec.get("module"))
            apply_genome(m, genome, spec.get("out_wire"))
            path = os.path.join(spec["out_dir"], f"cand_{name}.v")
            write(d, path)
            result["written"][name] = path
        except Exception as exc:       # one bad genome must not lose the others
            result["errors"][name] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: Optional[List[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    spec = json.loads(open(argv[0]).read())
    res = run_batch(spec)
    out = argv[1] if len(argv) > 1 else None
    text = json.dumps(res)
    if out:
        open(out, "w").write(text)
    else:
        sys.stdout.write("\n@@RESULT@@" + text + "\n")


if __name__ == "__main__":
    main()
