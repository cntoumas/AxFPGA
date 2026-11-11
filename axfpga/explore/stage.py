"""Pipeline stage: build every technique's space for one design and explore each one."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from axfpga.explore import lutnet
from axfpga.explore.search import BUDGET_FE, BUDGET_S, DEFAULT_ALGORITHM, Exploration
from axfpga.explore.techniques import (CatalogTechnique, LutTechnique, PpgDesignTechnique, RegTechnique,
                                       SiteOpsTechnique, Technique)

logger = logging.getLogger("axfpga.explore")

CATALOG_FAMILIES = (
    ("loa_cone", r"pyALS_loa_", "lower-part OR adder over the whole cone (one depth)"),
    ("ppg_cone", r"pyALS_ppg_", "partial-product truncation over the whole cone (one depth)"),
    ("compressors", r"pyALS_comp", "approximate LUT6_2 4:2 / 5:3 compressors over the whole cone"),
    ("nibble_cone", r"nibble", "4-bit aligned carry truncation over the whole cone"),
    ("pyals_prune", r"prune_lut_|cum_prune_", "LUT pruning to constant 0 (single and cumulative)"),
    ("out_prune", r"out_prune_", "output-bit pruning of the relaxed bus"),
    ("pyals_catalog", r"pyALS_", "pyALS catalog rewrite of every LUT (distance / input dropping)"),
)


def algorithm_from_env() -> str:
    return os.environ.get("AXFPGA_ALGORITHM", DEFAULT_ALGORITHM)


def techniques_from_env() -> Optional[List[str]]:
    v = os.environ.get("AXFPGA_TECHNIQUES", "").strip()
    return [t.strip() for t in v.split(",") if t.strip()] or None


def list_cone_sites(cone_v: Path, module: str, out_wire: Optional[str], work: Path) -> List[Dict[str, Any]]:
    work.mkdir(parents=True, exist_ok=True)
    sp, rp = work / "sites_spec.json", work / "sites.json"
    sp.write_text(json.dumps({"cone": str(cone_v), "module": module, "out_wire": out_wire, "list_sites": True,
                              "genomes": {}, "out_dir": str(work)}))
    proc = subprocess.run([sys.executable, "-m", "axfpga.explore.ops", str(sp), str(rp)], capture_output=True, text=True,
                          timeout=900)
    if proc.returncode != 0 or not rp.exists():
        raise RuntimeError(f"site listing failed: {proc.stderr[-400:]}")
    return json.loads(rp.read_text())["sites"]


def cone_techniques(ev: Any, cone_v: Path, meta_json: Optional[Path], work: Path, bound: float,
                    only: Optional[Sequence[str]] = None) -> List[Technique]:
    """Every cone-level technique with a non-empty space."""
    techs: List[Technique] = []
    want = (lambda n: only is None or n in only)
    try:
        sites = list_cone_sites(ev.exact_v, ev.top, ev.rel, work / "sites")
    except Exception as exc:
        logger.warning("site listing failed: %s", exc)
        sites = []
    for name in ("trunc", "loa", "nibble", "ppg"):
        if want(name):
            t = SiteOpsTechnique(name, sites, ev.exact_v, ev.top, ev.rel)
            if t.genes:
                techs.append(t)
    if want("pyals_lut"):
        try:
            from axfpga.pyals_bridge import DEFAULT_CATALOG_PATH, FpgaCatalogCache
            lut_v = lutnet.map_luts(ev.exact_v, ev.top, work / "lut_cone.v")
            net = lutnet.LutNet(lut_v, ev.rel)
            if net.sites:
                techs.append(LutTechnique(net, FpgaCatalogCache(str(DEFAULT_CATALOG_PATH))))
        except Exception as exc:
            logger.warning("LUT technique unavailable: %s", exc)
    fams = [f for f in CATALOG_FAMILIES if want(f[0])]
    if fams:
        try:
            from axfpga.pyals_bridge import PyAlsBridge
            bridge = PyAlsBridge(cone_verilog_path=cone_v, meta_json_path=meta_json, work_dir=work / "catalog", cut_size=4)
            cands = bridge.generate_catalog_candidates(bound=bound, exact_cone_path=ev.exact_v)
        except Exception as exc:
            logger.warning("whole-cone catalog failed: %s", exc)
            cands = []
        taken = set()
        for fam, pat, descr in fams:
            mine = [(c["name"], Path(c["path"])) for c in cands if c["name"] not in taken and re.search(pat, c["name"])]
            taken.update(n for n, _ in mine)
            if mine:
                techs.append(CatalogTechnique(fam, descr, sorted(mine)))
    return techs


def explore(techs: Sequence[Technique], evaluators: Dict[str, Any], work: Path, algorithm: str, jobs: int,
            primary: str, budget_fe: Optional[int] = None, budget_s: Optional[float] = None,
            seed: Optional[int] = None) -> List[Dict[str, Any]]:
    """Explore every technique; returns one summary per technique (best point, front, explanation, ...)."""
    budget_fe = budget_fe or int(os.environ.get("AXFPGA_BUDGET_FE", BUDGET_FE))
    budget_s = budget_s or float(os.environ.get("AXFPGA_BUDGET_S", BUDGET_S))
    seed = seed if seed is not None else int(os.environ.get("AXFPGA_SEED", 1))
    out = []
    for t in techs:
        ev = evaluators[t.level]
        t0 = time.time()
        try:
            s = Exploration(t, ev, work / t.name, algorithm=algorithm, budget_fe=budget_fe, budget_s=budget_s,
                            seed=seed, jobs=jobs, primary=primary).run()
        except Exception as exc:
            logger.warning("exploration of %s failed: %s", t.name, exc)
            continue
        b = s["best"]
        logger.info("[Explore] %-13s %-13s space %-10s evals %4d  best cost %s (base %d)  %s  %.0fs", t.name, s["strategy"],
                    s["space_size"], s["evaluations"], b["cost"] if b else "-", ev.base_cost,
                    (b["describe"][:70] if b else "no feasible point"), time.time() - t0)
        out.append(s)
    return out
