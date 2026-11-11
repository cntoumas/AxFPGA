"""Cone-level evaluation for the design-space exploration."""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import random
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from axfpga.cone_interface import parse_cone_interface
from axfpga.config import xilinx_cells_sim
from axfpga.estimator import FpgaEstimator

logger = logging.getLogger("axfpga.explore")

EXHAUSTIVE_BITS = 16
SAMPLES = 4096
SEED = 20261106
DSP_LC_WEIGHT = 20
# Candidates that instantiate Xilinx primitives (compressors: LUT6_2) need the Yosys simulation models to compile.
_XILINX_PRIM = re.compile(r"\b(LUT[1-6](?:_2)?|CARRY4|MUXF[78]|FDRE|FDSE|FDCE|FDPE|DSP48E1)\s*(#|\w+\s*\()")
LOWER_METRICS = ("wce", "med", "mred", "er", "rmse")


def error_metrics(e: np.ndarray, a: np.ndarray, width: int) -> Dict[str, float]:
    """Approximate-computing error metrics of decoded exact values ``e`` and approximate values ``a``."""
    d = a - e
    ad = np.abs(d)
    fs = float((1 << width) - 1) if width else 1.0
    nz = e != 0
    rel = ad[nz] / np.abs(e[nz]) if nz.any() else np.zeros(0)
    n = max(1, ad.size)
    med = float(ad.mean()) if ad.size else 0.0
    mse = float((d * d).mean()) if ad.size else 0.0
    wce = float(ad.max()) if ad.size else 0.0
    return {
        "er": float((ad > 0).sum() / n),
        "med": med,
        "nmed": med / fs,
        "mred": float(rel.mean()) if rel.size else 0.0,
        "wce": wce,
        "wce_pct": 100.0 * wce / fs,
        "wcre": float(rel.max()) if rel.size else 0.0,
        "mse": mse,
        "rmse": math.sqrt(mse),
        "bias": float(d.mean()) if ad.size else 0.0,
        "samples": int(ad.size),
    }


def violations(errs: Dict[str, float], limits: Dict[str, float]) -> Tuple[List[str], float]:
    """(messages, total normalized violation) of the lower-is-better limits."""
    msgs, total = [], 0.0
    for m, thr in limits.items():
        v = errs.get(m)
        if v is None or thr is None:
            continue
        if v > thr:
            msgs.append(f"{m} {v:.4g} > {thr:.4g}")
            total += (v - thr) / thr if thr > 0 else v + 1.0
    return msgs, total


class ConeEvaluator:
    """Error and cost of candidate cones against one exact cone."""

    def __init__(self, cone_v: Path, meta_json: Optional[Path], work_dir: Path, estimator: FpgaEstimator,
                 limits: Dict[str, float], samples: int = SAMPLES, seed: int = SEED,
                 exhaustive_bits: int = EXHAUSTIVE_BITS, exact_v: Optional[Path] = None):
        self.work = Path(work_dir)
        self.work.mkdir(parents=True, exist_ok=True)
        self.iface = parse_cone_interface(cone_v, meta_json)
        self.top = self.iface["module_name"]
        if exact_v is None:
            from axfpga.tournament import Tournament
            exact_v = Tournament(estimator, self.work)._write_exact_cone(self.iface)
        self.exact_v = Path(exact_v)
        self.estimator = estimator
        self.limits = {k: v for k, v in limits.items() if v is not None}
        self.rel = self.iface["relaxed_output"]
        info = self.iface["outputs"][self.rel]
        fmt = self.iface.get("relaxed_format")
        self.out_width = info["width"]
        self.fmt_kind = fmt["kind"] if fmt else ("s" if info.get("signed") else "u")
        self.fmt_width = fmt["width"] if fmt else info["width"]
        # inout ports (a cone can drive a bus it also reads) are connected once, as outputs
        self.inputs = [(n, i["width"]) for n, i in self.iface["inputs"].items() if n not in self.iface["outputs"]]
        self.in_bits = sum(w for _, w in self.inputs)
        self.exhaustive = self.in_bits <= exhaustive_bits
        self.n_vectors = (1 << self.in_bits) if self.exhaustive else samples
        self.stim = self.work / "explore_stim.hex"
        self._write_stimulus(seed)
        self._memo: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.evals = 0
        self.base = estimator.evaluate_netlist(self.exact_v, top_module=self.top)
        self.base_cost = self.base["estimated_lcs"] + DSP_LC_WEIGHT * self.base["dsps"]
        self.exact_vals = self._simulate(self.exact_v, "exact")
        if self.exact_vals is None:
            raise RuntimeError("exact cone does not simulate")

    # ------------------------------------------------------------------ stimulus / simulation
    def _write_stimulus(self, seed: int) -> None:
        rnd = random.Random(seed)
        lines = []
        if self.exhaustive:
            for v in range(self.n_vectors):
                parts, x = [], v
                for _, w in self.inputs:
                    parts.append(f"{x & ((1 << w) - 1):x}")
                    x >>= w
                lines.append(" ".join(parts))
        else:
            for _ in range(self.n_vectors):
                lines.append(" ".join(f"{rnd.getrandbits(w):x}" for _, w in self.inputs))
        self.stim.write_text("\n".join(lines) + "\n")

    def _tb(self, mod: str, out_file: Path) -> str:
        decl, conns, reads = [], [], []
        for i, (name, w) in enumerate(self.inputs):
            decl.append(f"  reg [{w - 1}:0] i{i};")
            conns.append(f".{name} (i{i})")
            reads.append(f"i{i}")
        for j, (name, info) in enumerate(self.iface["outputs"].items()):
            decl.append(f"  wire [{info['width'] - 1}:0] o{j};")
            conns.append(f".{name} (o{j})")
        rel_j = list(self.iface["outputs"]).index(self.rel)
        fmt = " ".join(["%h"] * len(self.inputs))
        return f"""`timescale 1ns/1ps
module tb_explore;
{chr(10).join(decl)}
  integer fd, fo, r;
  {mod} u ({", ".join(conns)});
  initial begin
    fd = $fopen("{self.stim}", "r");
    fo = $fopen("{out_file}", "w");
    while (!$feof(fd)) begin
      r = $fscanf(fd, "{fmt}\\n", {", ".join(reads)});
      if (r == {len(self.inputs)}) begin
        #1 $fdisplay(fo, "%h", o{rel_j});
      end
    end
    $fclose(fd); $fclose(fo);
    $finish;
  end
endmodule
"""

    def _simulate(self, cand_v: Path, tag: str) -> Optional[np.ndarray]:
        sim_dir = self.work / "sim"
        sim_dir.mkdir(exist_ok=True)
        uid = hashlib.sha1(f"{tag}{cand_v}{time.time_ns()}{threading.get_ident()}".encode()).hexdigest()[:12]
        mod = f"{self.top}_c{uid}"
        text = Path(cand_v).read_text(encoding="utf-8", errors="ignore")
        text = re.sub(rf"\bmodule\s+{re.escape(self.top)}\b", f"module {mod}", text, count=1)
        cv, tb, vvp, out = (sim_dir / f"{uid}.v", sim_dir / f"tb_{uid}.v", sim_dir / f"{uid}.vvp", sim_dir / f"{uid}.out")
        cv.write_text(text)
        tb.write_text(self._tb(mod, out))
        try:
            libs = [str(xilinx_cells_sim())] if _XILINX_PRIM.search(text) else []
            r = subprocess.run(["iverilog", "-g2012", "-o", str(vvp), "-s", "tb_explore", str(tb), str(cv)] + libs,
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                logger.debug("candidate %s does not compile: %s", cand_v, r.stderr[-300:])
                return None
            r = subprocess.run(["vvp", "-n", str(vvp)], capture_output=True, text=True, timeout=1800)
            if r.returncode != 0 or not out.exists():
                return None
            raw = [int(re.sub(r"[xXzZ]", "0", ln.strip()), 16) for ln in out.read_text().split() if ln.strip()]
        finally:
            for f in (cv, tb, vvp, out):
                f.unlink(missing_ok=True)
        if len(raw) != self.n_vectors:
            return None
        return self._decode(raw)

    def _decode(self, raw: Sequence[int]) -> np.ndarray:
        w = self.fmt_width
        mask = (1 << w) - 1
        vals = []
        for x in raw:
            x &= mask
            if self.fmt_kind == "s" and w > 0:
                vals.append(float(x - (1 << w) if (x >> (w - 1)) & 1 else x))
            elif self.fmt_kind == "sm" and w > 1:
                mag = x & ((1 << (w - 1)) - 1)
                vals.append(float(-mag if (x >> (w - 1)) & 1 else mag))
            else:
                vals.append(float(x))
        return np.array(vals, dtype=np.float64)

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, cand_v: Path, synth_infeasible: bool = False) -> Dict[str, Any]:
        """Errors, limit violations and (when feasible) cost of one candidate cone."""
        text = Path(cand_v).read_text(encoding="utf-8", errors="ignore")
        key = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            hit = self._memo.get(key)
        if hit is not None and (hit["cost"] is not None or not synth_infeasible):
            return dict(hit, cached=True)
        t0 = time.time()
        vals = self._simulate(Path(cand_v), "cand")
        if vals is None:
            rec = {"ok": False, "feasible": False, "errors": None, "violation": 1e6, "violations": ["does not simulate"],
                   "cost": None, "lcs": None, "luts": None, "dsps": None, "carry4": None}
        else:
            errs = error_metrics(self.exact_vals, vals, self.fmt_width)
            msgs, viol = violations(errs, self.limits)
            rec = {"ok": True, "feasible": not msgs, "errors": errs, "violation": viol, "violations": msgs,
                   "cost": None, "lcs": None, "luts": None, "dsps": None, "carry4": None}
            if not msgs or synth_infeasible:
                try:
                    c = self.estimator.evaluate_netlist(Path(cand_v), top_module=self.top)
                    rec.update(lcs=c["estimated_lcs"], luts=c["total_luts"], dsps=c["dsps"], carry4=c["carry4"],
                               cost=c["estimated_lcs"] + DSP_LC_WEIGHT * c["dsps"])
                except Exception as exc:
                    rec.update(ok=False, feasible=False, violations=[f"synthesis failed: {exc}"[:200]], violation=1e6)
        rec["key"] = key
        rec["eval_s"] = round(time.time() - t0, 3)
        with self._lock:
            self._memo[key] = rec
            self.evals += 1
        return dict(rec, cached=False)

    def fitness(self, rec: Dict[str, Any]) -> float:
        """Objective value (minimized): cost when feasible, else the exact cost inflated by the violation."""
        if rec.get("feasible") and rec.get("cost") is not None:
            return float(rec["cost"])
        return float(self.base_cost) * (2.0 + min(rec.get("violation", 1e6), 1e3))
