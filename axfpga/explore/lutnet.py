"""LUT-level sites of a cone: its 4-LUT mapping, and per-LUT approximate rewrites from the pyALS catalog."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from axfpga.config import yosys_cmd

LUT_PAT = re.compile(r"assign\s+(\S+(?:\s*\[\d+\])?)\s*=\s*(\d+)'h([0-9a-fA-F]+)\s*>>\s*\{([^}]+)\};")
ASSIGN_PAT = re.compile(r"assign\s+(\S+(?:\s*\[\d+\])?)\s*=\s*([^;]+);")
GENE_NAMES = ("exact", "dist1", "dist2", "drop", "zero")
MAX_LUT_SITES = 48


def _tok(s: str) -> str:
    return re.sub(r"\s+", "", s)


def map_luts(cone_v: Path, top: str, out_v: Path) -> Path:
    script = (f"read_verilog -sv {cone_v}; hierarchy -check -top {top}; proc; opt_clean; techmap; opt; "
              f"abc -lut 4; opt_clean; write_verilog -noattr {out_v}")
    res = subprocess.run(yosys_cmd() + ["-q", "-p", script], capture_output=True, text=True)
    if res.returncode != 0 or not out_v.exists():
        raise RuntimeError(f"LUT mapping failed: {res.stderr[-300:]}")
    return out_v


class LutNet:
    """The 4-LUT mapping of a cone and its ranked LUT sites."""

    def __init__(self, lut_v: Path, relaxed_output: str, max_sites: int = MAX_LUT_SITES):
        self.path = Path(lut_v)
        self.content = self.path.read_text(encoding="utf-8")
        self.matches = list(LUT_PAT.finditer(self.content))
        self.rel = relaxed_output.lstrip("\\")
        self.sites = self._rank(max_sites)

    def _rank(self, max_sites: int) -> List[Dict[str, Any]]:
        lut_of: Dict[str, int] = {}
        inputs: Dict[int, List[str]] = {}
        for i, m in enumerate(self.matches):
            lut_of[_tok(m.group(1))] = i
            inputs[i] = [_tok(x) for x in m.group(4).split(",")]
        alias: Dict[str, List[str]] = {}
        for m in ASSIGN_PAT.finditer(self.content):
            lhs = _tok(m.group(1))
            if lhs in lut_of:
                continue
            rhs = m.group(2)
            if "'h" in rhs and ">>" in rhs:
                continue
            toks = [_tok(t) for t in re.split(r"[{},~&|^()\s]+", rhs) if t and not re.match(r"^\d+'[bhd]", t)]
            alias[lhs] = toks
        # significance: highest relaxed-output bit whose fan-in cone holds the LUT
        sig: Dict[int, int] = {}
        out_re = re.compile(rf"^\\?{re.escape(self.rel)}\[(\d+)\]$")
        roots = []
        for name in list(lut_of) + list(alias):
            mm = out_re.match(name)
            if mm:
                roots.append((int(mm.group(1)), name))
        for bit, root in sorted(roots):
            seen, stack = set(), [root]
            while stack:
                t = stack.pop()
                if t in seen:
                    continue
                seen.add(t)
                if t in lut_of:
                    i = lut_of[t]
                    sig[i] = max(sig.get(i, -1), bit)
                    stack.extend(inputs[i])
                elif t in alias:
                    stack.extend(alias[t])
        order = sorted(range(len(self.matches)), key=lambda i: (sig.get(i, 10 ** 6), i))
        return [{"id": f"lut@{i}", "index": i, "significance": sig.get(i), "kind": "lut", "max_k": len(GENE_NAMES) - 1,
                 "inputs": len(inputs[i])} for i in order[:max_sites]]

    def apply(self, genes: Dict[int, int], cache: Any) -> Tuple[str, int]:
        """Rewritten netlist text and the number of LUTs changed. ``genes``: LUT index -> gene value."""
        out = self.content
        changed = 0
        for idx in sorted(genes, reverse=True):
            g = int(genes[idx])
            if g <= 0:
                continue
            m = self.matches[idx]
            wire, wbits, hexs, ins = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            spec = bin(int(hexs, 16))[2:].zfill(wbits)[::-1]
            new = None
            if g in (1, 2):
                cand = cache.get_lut_at_dist(spec, g)
                if cand:
                    new = cand[0][::-1]
            elif g == 3:
                cand = cache.get_input_dropping_approx_luts(spec, max_dist=2)
                if cand:
                    new = cand[0]["synth_spec"][::-1]
            elif g == 4:
                new = "0" * wbits
            if new is None:
                continue
            nh = f"{int(new, 2):0{max(1, wbits // 4)}x}"
            rep = f"assign {wire} = {wbits}'h{nh} >> {{{ins}}};"
            out = out[:m.start()] + rep + out[m.end():]
            changed += 1
        return out, changed
