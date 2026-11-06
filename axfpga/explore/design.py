"""Design-level evaluation: for techniques that change the whole design (array-multiplier PPG depth, truncation behind
internal registers). Error on the relaxed top output from a random-vector simulation of the whole design, cost from a
full Yosys synthesis. Same record and fitness as ``ConeEvaluator``."""

from __future__ import annotations

import hashlib
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from axfpga.activity_power import run_vector_sim
from axfpga.estimator import FpgaEstimator
from axfpga.explore.evaluate import DSP_LC_WEIGHT, error_metrics, violations
from axfpga.quality.metrics import decoded

DESIGN_VECTORS = 2048


class DesignEvaluator:
    def __init__(self, exact_top_v: Path, top: str, work_dir: Path, estimator: FpgaEstimator, limits: Dict[str, float],
                 sim_kw: Dict[str, Any], fmt: Any, wire: str, skip: int, normalize: Callable[[Path, Path], Path],
                 base: Dict[str, Any], n_vectors: int = DESIGN_VECTORS, seed: int = 20261106,
                 vectors: Optional[List[Dict[str, int]]] = None):
        self.work = Path(work_dir)
        self.work.mkdir(parents=True, exist_ok=True)
        self.top, self.estimator, self.fmt, self.wire, self.skip = top, estimator, fmt, wire, skip
        self.limits = {k: v for k, v in limits.items() if v is not None}
        self.normalize = normalize
        self.sim_kw = dict(sim_kw, work_dir=self.work / "sim")
        if vectors:
            # the design's declared input stimulus (its inputs only; the application metric is not used here)
            self.vectors = list(vectors[:n_vectors])
            self.stimulus = "declared stimulus"
        else:
            rnd = random.Random(seed)
            self.vectors = [{n: rnd.getrandbits(w) for n, w in sim_kw["inputs"]} for _ in range(n_vectors)]
            self.stimulus = "random vectors"
        self.base = base
        self.base_cost = base["estimated_lcs"] + DSP_LC_WEIGHT * base["dsps"]
        self.exact_rows = self._sim(exact_top_v, "exact")
        if self.exact_rows is None:
            raise RuntimeError("exact design does not simulate")
        self._memo: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.evals = 0
        self.top_name = top

    def _sim(self, top_v: Path, tag: str) -> Optional[List[Dict[str, int]]]:
        uid = hashlib.sha1(f"{tag}{top_v}{time.time_ns()}{threading.get_ident()}".encode()).hexdigest()[:10]
        try:
            norm = self.normalize(Path(top_v), self.work / f"norm_{uid}.v")
            res = run_vector_sim([norm], vectors=self.vectors, tag=f"d{uid}", **self.sim_kw)
            norm.unlink(missing_ok=True)
            return res["outputs"]
        except Exception:
            return None

    def evaluate(self, cand_v: Path, synth_infeasible: bool = False) -> Dict[str, Any]:
        text = Path(cand_v).read_text(encoding="utf-8", errors="ignore")
        key = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
            hit = self._memo.get(key)
        if hit is not None:
            return dict(hit, cached=True)
        t0 = time.time()
        rows = self._sim(Path(cand_v), "cand")
        rec: Dict[str, Any] = {"cost": None, "lcs": None, "luts": None, "dsps": None, "carry4": None}
        if rows is None:
            rec.update(ok=False, feasible=False, errors=None, violation=1e6, violations=["does not simulate"])
        else:
            n = min(len(rows), len(self.exact_rows))
            e = decoded(self.fmt, self.exact_rows, self.wire, self.skip, n)
            a = decoded(self.fmt, rows, self.wire, self.skip, n)
            errs = error_metrics(e, a, getattr(self.fmt, "width", 0) or 0)
            msgs, viol = violations(errs, self.limits)
            rec.update(ok=True, feasible=not msgs, errors=errs, violation=viol, violations=msgs)
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
        if rec.get("feasible") and rec.get("cost") is not None:
            return float(rec["cost"])
        return float(self.base_cost) * (2.0 + min(rec.get("violation", 1e6), 1e3))
