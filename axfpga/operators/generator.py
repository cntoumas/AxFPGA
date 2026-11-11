"""Tournament generator for the RTL-level operators (axfpga.operators.core) with greedy stacking."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("axfpga.operators")

UNIFORM_KS = (1, 2, 3, 4, 5, 6, 8, 10, 12)
SINGLE_KS = (2, 4, 8)
OUT_KS = (1, 2, 3, 4, 5, 6, 8)
STEP_KS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 16)      # the depths a greedy step moves through
MAX_SITES = 16                                   # sites per design that take part (largest first)
SINGLE_SITES = 6                                 # sites that get single-site candidates
REFINE_EVALS = 24
REFINE_EVALS_LARGE = 12                          # cones above LARGE_LCS: one synthesis costs tens of seconds
LARGE_LCS = 2000


def run_operators(cone: Path, module: str, out_wire: Optional[str], genomes: Dict[str, Dict[str, Any]],
                  out_dir: Path, want_sites: bool = False, timeout: int = 900) -> Dict[str, Any]:
    """Apply genomes to the cone in a subprocess (see axfpga.operators.core). Returns the batch result."""
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = {"cone": str(cone), "module": module, "out_wire": out_wire, "list_sites": want_sites,
            "genomes": genomes, "out_dir": str(out_dir)}
    sp, rp = out_dir / "ops_spec.json", out_dir / "ops_result.json"
    sp.write_text(json.dumps(spec))
    rp.unlink(missing_ok=True)
    proc = subprocess.run([sys.executable, "-m", "axfpga.operators.core", str(sp), str(rp)],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not rp.exists():
        raise RuntimeError(f"operator batch failed (rc {proc.returncode}): {proc.stderr[-400:]}")
    return json.loads(rp.read_text())


def ranked_sites(sites: Sequence[Dict[str, Any]], max_sites: int = MAX_SITES) -> List[Dict[str, Any]]:
    """Arithmetic sites that can be truncated, heaviest first (ties by id), at most ``max_sites``."""
    arith = [s for s in sites if s["kind"] != "out" and s["max_k"] >= 1]
    arith.sort(key=lambda s: (-s["weight"], s["id"]))
    return arith[:max_sites]


def build_menu(sites: Sequence[Dict[str, Any]], max_sites: int = MAX_SITES, comps: Sequence[str] = ("zero", "mid", "max"),
               stacking: bool = True) -> Dict[str, Dict[str, Any]]:
    """Name -> genome of the first candidate menu. Without ``stacking`` only single-gene candidates are offered."""
    top = ranked_sites(sites, max_sites)
    out = next((s for s in sites if s["kind"] == "out"), None)
    arith_comps = [c for c in ("zero", "mid") if c in comps]
    menu: Dict[str, Dict[str, Any]] = {}
    if stacking:
        for comp in arith_comps:
            for k in UNIFORM_KS:
                g = {s["id"]: {"k": min(k, s["max_k"]), "comp": comp} for s in top}
                if g:
                    menu[f"trunc_all_k{k}_{comp}"] = g
    for r, s in enumerate(top[:SINGLE_SITES]):
        for k in SINGLE_KS:
            if k <= s["max_k"]:
                for comp in arith_comps:
                    menu[f"trunc_s{r}_k{k}_{comp}"] = {s["id"]: {"k": k, "comp": comp}}
    if out is not None:
        for k in OUT_KS:
            if k <= out["max_k"]:
                for comp in comps:
                    menu[f"out_k{k}_{comp}"] = {out["id"]: {"k": k, "comp": comp}}
    return menu


def next_k(k: int, cap: int) -> Optional[int]:
    """The next depth above ``k`` in STEP_KS, limited by ``cap`` (the last step is the cap itself)."""
    for s in STEP_KS:
        if s > k:
            return min(s, cap) if min(s, cap) > k else None
    return cap if cap > k else None


def neighbours(genome: Dict[str, Dict[str, Any]], sites: Sequence[Dict[str, Any]],
               comps: Sequence[str] = ("zero", "mid", "max")) -> Dict[str, Dict[str, Any]]:
    """Genomes one greedy step away: one gene deepened, or one new gene at the smallest step."""
    out: Dict[str, Dict[str, Any]] = {}
    for s in list(ranked_sites(sites)) + [s for s in sites if s["kind"] == "out"]:
        cur = genome.get(s["id"])
        offered = [c for c in comps if c in (("zero", "mid") if s["kind"] != "out" else ("zero", "mid", "max"))]
        comps_here = [cur["comp"]] if cur else offered
        k = next_k(cur["k"] if cur else 0, s["max_k"])
        if k is None:
            continue
        for comp in comps_here:
            g = {sid: dict(gene) for sid, gene in genome.items()}
            g[s["id"]] = {"k": k, "comp": comp}
            out[f"{s['id']}|k{k}|{comp}"] = g
    return out


class OperatorGenerator:
    """Callable Tournament generator: ``gen(iface, work_dir, bound) -> candidates``; ``gen.refine(...)`` stacks."""

    def __init__(self, max_sites: int = MAX_SITES, enabled: bool = True, comps: Sequence[str] = ("zero", "mid", "max"),
                 stacking: bool = True):
        self.max_sites = max_sites
        self.enabled = enabled
        self.comps = tuple(comps)          # correction constants offered: ("zero",) is plain truncation (ablation)
        self.stacking = stacking           # False: single-gene candidates only (no "all sites" menu, no greedy stage)
        self.sites: List[Dict[str, Any]] = []
        self.genomes: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def from_env(cls) -> "OperatorGenerator":
        """AXFPGA_OPS: ``all`` (default), ``nocomp`` (plain truncation), ``nostack`` (no stacking), ``off``."""
        mode = os.environ.get("AXFPGA_OPS", "all").strip().lower()
        if mode == "off":
            return cls(enabled=False)
        if mode == "nocomp":
            return cls(comps=("zero",))
        if mode == "nostack":
            return cls(stacking=False)
        if mode == "nocomp-nostack":
            return cls(comps=("zero",), stacking=False)
        if mode != "all":
            raise ValueError(f"AXFPGA_OPS={mode!r}: expected all, nocomp, nostack, nocomp-nostack or off")
        return cls()

    def __repr__(self) -> str:
        return "OperatorGenerator()"

    def __call__(self, iface: Dict[str, Any], work_dir: Path, bound: float) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        cone = Path(iface["exact_netlist"])
        out_wire = iface.get("relaxed_output")
        odir = Path(work_dir) / "operators"
        first = run_operators(cone, iface["module_name"], out_wire, {}, odir, want_sites=True)
        self.sites = first["sites"]
        menu = build_menu(self.sites, self.max_sites, self.comps, self.stacking)
        res = run_operators(cone, iface["module_name"], out_wire, menu, odir)
        for name, err in res["errors"].items():
            logger.warning("operator candidate %s failed: %s", name, err)
        self.genomes = {n: menu[n] for n in res["written"]}
        return [{"name": f"op_{n}", "path": Path(p), "strategy": "operators", "genome": menu[n]}
                for n, p in sorted(res["written"].items())]

    def refine(self, iface: Dict[str, Any], work_dir: Path, bound: float, valid: List[Dict[str, Any]],
               score_many: Callable[[Sequence[Dict[str, Any]]], List[Optional[Dict[str, Any]]]], base_lcs: int) -> List[Dict[str, Any]]:
        """Greedy stacking from the cheapest operator candidate. ``score_many`` evaluates candidates (the Tournament's"""
        if not self.enabled or not self.stacking or not self.sites:
            return []
        starts = [v for v in valid if "genome" in v and v["lcs"] < base_lcs]
        if not starts:
            return []
        cur = min(starts, key=lambda v: (v["lcs"], v["error"], v["name"]))
        budget = REFINE_EVALS_LARGE if base_lcs > LARGE_LCS else REFINE_EVALS
        cone, out_wire = Path(iface["exact_netlist"]), iface.get("relaxed_output")
        odir = Path(work_dir) / "operators"
        found: List[Dict[str, Any]] = []
        tried = set()
        step = 0
        while budget > 0:
            nbrs = {n: g for n, g in neighbours(cur["genome"], self.sites, self.comps).items()
                    if json.dumps(g, sort_keys=True) not in tried}
            if not nbrs:
                break
            # name each neighbour by its position so the files do not collide across rounds
            named = {f"stk{step}_{i:02d}": g for i, (_, g) in enumerate(sorted(nbrs.items()))}
            res = run_operators(cone, iface["module_name"], out_wire, named, odir)
            batch = [(name, g) for name, g in named.items() if name in res["written"]][:budget]
            for _, g in batch:
                tried.add(json.dumps(g, sort_keys=True))
            budget -= len(batch)
            recs = score_many([{"name": f"op_{name}", "path": Path(res["written"][name]), "strategy": "operators", "genome": g}
                               for name, g in batch])
            best = None
            for rec in recs:
                if rec is None:
                    continue
                found.append(rec)
                if rec["lcs"] < cur["lcs"] and (best is None or (rec["lcs"], rec["error"], rec["name"]) < (best["lcs"], best["error"], best["name"])):
                    best = rec
            if best is None:
                break
            logger.info("operator stacking step %d: cone LC %d -> %d (error %.4g)", step, cur["lcs"], best["lcs"], best["error"])
            cur = best
            step += 1
        return found
