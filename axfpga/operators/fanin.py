"""Structural fan-in hashes of signals (property check: approximation never changes what a precise signal depends on)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from typing import Any, Dict, List, Sequence, Tuple

ROUNDS = 40


def _h(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:20]


def compute(verilog: str, top: str, signals: Sequence[str], rounds: int = ROUNDS) -> Dict[str, str]:
    from pyosys import libyosys as ys
    d = ys.Design()
    ys.run_pass(f"read_verilog -sv {verilog}; hierarchy -check -top {top}; proc; flatten; opt_clean", d)
    m = d.top_module()

    def key(b: Any) -> Tuple[Any, ...]:
        return ("const", str(b.data)) if b.wire is None else (b.wire.name.str(), b.offset)

    parent: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}

    def find(x: Tuple[Any, ...]) -> Tuple[Any, ...]:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for lhs, rhs in m.connections():              # wire aliases (assigns): one canonical bit per alias class
        for bl, br in zip(lhs.bits(), rhs.bits()):
            kl, kr = key(bl), key(br)
            if kr[0] == "const" and kl[0] != "const":
                parent[find(kl)] = kr             # a constant is always the canonical bit of its class
            elif kl[0] != "const":
                a_, b_ = find(kl), find(kr)
                if a_ != b_:
                    parent[a_] = b_

    def canon(b: Any) -> Tuple[Any, ...]:
        return find(key(b))

    cells: List[Any] = [c for c in m.cells_.values() if c.type.str() != "$scopeinfo"]
    driver: Dict[Tuple[Any, ...], Tuple[int, str, int]] = {}      # canonical bit -> (cell index, port, bit index)
    base: List[str] = []
    ins: List[List[Tuple[str, int, Tuple[Any, ...]]]] = []         # per cell: (port, bit index, canonical bit) of input bits
    for i, c in enumerate(cells):
        params = sorted((p.str(), str(v.as_string()) if hasattr(v, "as_string") else str(v)) for p, v in c.parameters.items())
        base.append(_h(c.type.str(), params))
        row = []
        for port, sig in c.connections().items():
            for k, b in enumerate(sig.bits()):
                cb = canon(b)
                if c.output(port):
                    driver[cb] = (i, port.str(), k)
                else:
                    row.append((port.str(), k, cb))
        ins.append(sorted(row, key=lambda r: (r[0], r[1])))

    def leaf(cb: Tuple[Any, ...]) -> str:
        return f"const:{cb[1]}" if cb[0] == "const" else f"in:{cb[0]}[{cb[1]}]"

    def bit_label(cb: Tuple[Any, ...], labels: List[str]) -> str:
        if cb in driver:
            i, port, k = driver[cb]
            return _h(labels[i], port, k)
        return leaf(cb)

    labels = list(base)
    for _ in range(rounds):
        labels = [_h(labels[i], *[(p, k, bit_label(cb, labels)) for p, k, cb in ins[i]]) for i in range(len(cells))]

    out: Dict[str, str] = {}
    for name in signals:
        w = m.wire(ys.IdString("\\" + name.lstrip("\\")))
        if w is None:
            raise KeyError(f"signal {name} not found in {top}")
        out[name] = _h(*[bit_label(canon(b), labels) for b in ys.SigSpec(w).bits()])
    return out


def precise_signals(verilog: str, top: str) -> List[str]:
    """Names of the top-level ports that must stay precise: ``axc_precise`` ports and primary outputs that are not"""
    from pyosys import libyosys as ys
    d = ys.Design()
    ys.run_pass(f"read_verilog -sv {verilog}; hierarchy -check -top {top}", d)
    m = d.top_module()
    prec, rel = ys.IdString("\\axc_precise"), ys.IdString("\\axc_relax")
    names = []
    for wid, w in m.wires_.items():
        if w.has_attribute(prec) or (w.port_output and not w.has_attribute(rel)):
            names.append(wid.str().lstrip("\\"))
    return sorted(names)


def fanin_hashes(verilog: str, top: str, signals: Sequence[str], rounds: int = ROUNDS) -> Dict[str, str]:
    """``compute`` in a subprocess (keeps libyosys state out of the calling process)."""
    proc = subprocess.run([sys.executable, "-m", "axfpga.operators.fanin", str(verilog), top, *signals],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"fanin hash failed: {proc.stderr[-400:]}")
    return json.loads(proc.stdout.rsplit("@@RESULT@@", 1)[1].splitlines()[0])


if __name__ == "__main__":
    res = compute(sys.argv[1], sys.argv[2], sys.argv[3:])
    sys.stdout.write("\n@@RESULT@@" + json.dumps(res) + "\n")
