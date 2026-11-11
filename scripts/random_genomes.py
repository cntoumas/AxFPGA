#!/usr/bin/env python3
"""Random-genome check (Phase 5 gate): do random operator genomes synthesize, and is the control proof ever falsified?

For every design of a finished suite run (``--build-dir``, which holds each design's extracted cone, top shell and exact
netlists) this draws ``--n`` random genomes over the design's automatically enumerated sites (1 to 6 genes: operand
truncation at a random depth and correction on arithmetic sites, output truncation), applies them to the cone, stitches
the approximate cone back into the top shell and checks:

  synth    the cone synthesizes with ``synth_xilinx`` (and, for designs of at most ``--full-synth-luts`` LUTs, so does the
           whole reintegrated design); the reintegrated design elaborates (``hierarchy -check; proc; check -assert``)
  control  Yosys SAT control-output equivalence of the reintegrated design against the exact design (never FALSIFIED)
  wce      Yosys SAT worst-case bound of the cone against the design's own WCE limit (PROVEN, FALSIFIED, TIMEOUT, ERROR)

Genomes are random, so many exceed the error limit: WCE FALSIFIED is expected and is not a failure. A control proof
FALSIFIED, or a genome that does not synthesize, is. ``--budget-s`` caps the wall time per design; the report states
how many genomes were processed.

Writes ``random_genomes.json`` and ``random_genomes.md`` into ``--out-dir``.
"""

from __future__ import annotations

import argparse
import functools
import json
import multiprocessing as mp
import random
import re
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

SEED = 20261105


def first_module(path: Path) -> str:
    m = re.search(r"^module\s+(\S+?)\s*\(", path.read_text(), re.M)
    if not m:
        raise ValueError(f"no module in {path}")
    return m.group(1)


def draw_genomes(sites: List[Dict[str, Any]], n: int, rng: random.Random) -> List[Dict[str, Any]]:
    arith = [s for s in sites if s["kind"] != "out" and s["max_k"] >= 1]
    out = [s for s in sites if s["kind"] == "out"]
    pool = arith + out
    genomes = []
    for _ in range(n):
        genes = rng.sample(pool, rng.randint(1, min(6, len(pool))))
        g = {}
        for s in genes:
            comps = ("zero", "mid", "max")
            g[s["id"]] = {"k": rng.randint(1, min(s["max_k"], 12)), "comp": rng.choice(comps)}
        genomes.append(g)
    return genomes


@functools.lru_cache(maxsize=None)
def _base_problems(exact_top: str, top: str) -> int:
    return check_problems(Path(exact_top), top) or 0


def check_problems(path: Path, top: str) -> Optional[int]:
    """Problems reported by Yosys ``check`` after ``proc`` (None if the design does not elaborate)."""
    import subprocess
    from axfpga.config import yosys_cmd
    r = subprocess.run(yosys_cmd() + ["-p", f"read_verilog -sv {path}; hierarchy -check -top {top}; proc; check"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    m = re.findall(r"Found and reported (\d+) problems", r.stdout)
    return int(m[-1]) if m else 0


def elaboration(final: Path, top: str, exact_top: str) -> Dict[str, Any]:
    """``elaborates``: ``hierarchy -check; proc`` succeed. ``new_check_problems``: more ``check`` problems than the exact"""
    n = check_problems(final, top)
    if n is None:
        return {"elaborates": False, "error": "elaboration failed"}
    base = _base_problems(str(exact_top), top)
    return {"elaborates": True, "new_check_problems": max(0, n - base)}


def check_one(job: Dict[str, Any]) -> Dict[str, Any]:
    """Reintegrate, synthesize and prove one genome (runs in a worker process of its own design)."""
    from axfpga.estimator import FpgaEstimator
    from axfpga.pipeline import FORMAL_TIMEOUT_S, _reintegrate  # noqa: F401
    from axfpga.verifier import FormalControlVerifier, FormalWceVerifier

    t0 = time.time()
    rec: Dict[str, Any] = {"idx": job["idx"], "genes": len(job["genome"]), "genome": job["genome"]}
    work = Path(job["work"])
    work.mkdir(parents=True, exist_ok=True)
    final = work / f"final_{job['idx']:03d}.v"
    try:
        bench = {"top": job["top"], "files": [Path(f) for f in job["files"]]}
        _reintegrate(bench, work, Path(job["shell"]), Path(job["cone"]), Path(job["approx_cone"]), final)
        rec["reintegrated"] = final.exists()
    except Exception as exc:
        rec.update(reintegrated=False, error=f"reintegration: {exc}")
        rec["secs"] = round(time.time() - t0, 1)
        return rec
    est = FpgaEstimator(family="xc7", device="xc7z010", nodsp=job["nodsp"])
    try:
        cone_res = est.evaluate_netlist(job["approx_cone"], top_module=job["cone_module"])
        rec["cone_lcs"] = cone_res["estimated_lcs"]
        rec["synth_cone"] = True
    except Exception as exc:
        rec.update(synth_cone=False, error=f"cone synthesis: {str(exc)[:160]}")
    rec.update(elaboration(final, job["top"], job["exact_top"]))
    if job["full_synth"]:
        try:
            res = est.evaluate_netlist(final, top_module=job["top"])
            rec["full_lcs"], rec["synth_full"] = res["estimated_lcs"], True
        except Exception as exc:
            rec.update(synth_full=False, error=f"full synthesis: {str(exc)[:160]}")
    try:
        rec["control"] = FormalControlVerifier(exact_vlog=Path(job["exact_top"]), approx_vlog=final, meta_json=Path(job["meta"]),
                                               timeout=job["proof_timeout"]).verify().get("status", "ERROR")
    except Exception as exc:
        rec["control"] = "ERROR"
        rec.setdefault("error", f"control proof: {str(exc)[:160]}")
    try:
        rec["wce"] = FormalWceVerifier(exact_cone_vlog=Path(job["exact_cone"]), approx_cone_vlog=Path(job["approx_cone"]),
                                       meta_json=Path(job["meta"]), timeout=job["proof_timeout"]).verify().get("status", "ERROR")
    except Exception as exc:
        rec["wce"] = "ERROR"
    rec["secs"] = round(time.time() - t0, 1)
    return rec


def run_design(args: Dict[str, Any]) -> Dict[str, Any]:
    """One design: sites, genomes, checks (own process: libyosys state)."""
    from axfpga.operators import run_operators
    d = Path(args["dir"])
    meta = json.loads((d / "cone_meta.json").read_text())
    result = json.loads((d / "result.json").read_text())
    summary: Dict[str, Any] = {"id": d.name, "status": meta.get("status"), "requested": args["n"], "processed": 0, "rows": []}
    if meta.get("status") != "SUCCESS":
        summary["note"] = f"no cone ({meta.get('status')})"
        return summary
    extracted = d / "extracted_cone.v"
    clean = d / "pyals_run" / "exact_cone_clean.v"
    cone = clean if clean.exists() else extracted
    mod = first_module(cone)
    out_wire = (meta.get("submodule_outputs") or [{}])[0].get("name")
    work = Path(args["out"]) / d.name
    sites = run_operators(cone, mod, out_wire, {}, work / "ops", want_sites=True)["sites"]
    summary["sites"] = len(sites)
    rng = random.Random(f"{SEED}:{d.name}")
    genomes = draw_genomes(sites, args["n"], rng)
    named = {f"r{i:03d}": g for i, g in enumerate(genomes)}
    batch = run_operators(cone, mod, out_wire, named, work / "ops")
    summary["apply_errors"] = batch["errors"]
    files = sorted((d / "annotated_rtl").glob("*.v")) + sorted((d / "annotated_rtl").glob("*.sv"))
    # the reintegration helper copies untouched sub-modules from the original file list: use the design's patched/annotated files
    jobs = []
    full = result["exact"]["luts"] <= args["full_synth_luts"]
    for i, g in enumerate(genomes):
        name = f"r{i:03d}"
        if name not in batch["written"]:
            continue
        jobs.append({"idx": i, "genome": g, "work": str(work / "runs"), "top": meta["top_module"], "files": [str(f) for f in files],
                     "shell": str(d / "top_shell.v"), "cone": str(extracted), "approx_cone": batch["written"][name],
                     "exact_cone": str(cone), "cone_module": mod,
                     "exact_top": str(d / "exact_baseline_top.v"), "meta": str(d / "cone_meta.json"),
                     "nodsp": not result.get("dsp_inference", True), "full_synth": full, "proof_timeout": args["proof_timeout"]})
    t0 = time.time()
    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args["inner"]) as pool:
        futs = []
        for j in jobs:
            futs.append(pool.submit(lambda j=j: check_one(j) if time.time() - t0 < args["budget_s"] else None))
        for f in futs:
            r = f.result()
            if r is not None:
                rows.append(r)
    summary["rows"] = rows
    summary["processed"] = len(rows)
    summary["wall_s"] = round(time.time() - t0, 1)
    return summary


def aggregate(s: Dict[str, Any]) -> Dict[str, Any]:
    rows = s.get("rows", [])
    ctrl, wce = Counter(r.get("control", "-") for r in rows), Counter(r.get("wce", "-") for r in rows)
    return {"id": s["id"], "status": s.get("status"), "sites": s.get("sites"), "requested": s["requested"], "processed": s["processed"],
            "reintegrated": sum(1 for r in rows if r.get("reintegrated")),
            "cone_synth_ok": sum(1 for r in rows if r.get("synth_cone")),
            "elaborates": sum(1 for r in rows if r.get("elaborates")),
            "full_synth_ok": sum(1 for r in rows if r.get("synth_full")), "full_synth_run": sum(1 for r in rows if "synth_full" in r),
            "control": dict(ctrl), "wce": dict(wce), "wall_s": s.get("wall_s"), "note": s.get("note"),
            "apply_errors": len(s.get("apply_errors", {}))}


def write_report(results: List[Dict[str, Any]], od: Path, proof_timeout: int, full_synth_luts: int) -> int:
    """random_genomes.json / .md from the per-design results; returns the number of FALSIFIED control proofs."""
    results = sorted(results, key=lambda s: s["id"])
    (od / "random_genomes.json").write_text(json.dumps(results, indent=1, default=str))
    aggs = [aggregate(s) for s in results]
    md = ["# Random-genome check", "",
          f"Genomes are drawn over each design's enumerated sites (seed {SEED}). `synth` = the approximate cone synthesizes; "
          "`elab` = the reintegrated design elaborates (`hierarchy -check; proc`); `new problems` = reintegrated designs with more Yosys `check` "
          "problems than the exact design; `full` = the whole reintegrated design synthesizes (run for designs of at most "
          f"{full_synth_luts} LUTs). Control and WCE proofs have a {proof_timeout} s timeout; control BOUNDED means the induction did not close "
          "and the bounded check passed. WCE FALSIFIED is expected for random genomes (they exceed the error limit); control FALSIFIED is a "
          "failure. WCE `other` = no cone-level proof applies (the design's relaxed output is not a cone output).", "",
          "| Design | Sites | Processed / requested | Reintegrated | Cone synth | Elab | New problems | Full synth | Control PROVEN | Control FALSIFIED | Control other | WCE PROVEN | WCE FALSIFIED | WCE TIMEOUT | WCE other |",
          "|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    tot = Counter()
    for a, s in zip(aggs, results):
        c, w = a["control"], a["wce"]
        proven, fals = c.get("PROVEN", 0), c.get("FALSIFIED", 0)
        other_c = a["processed"] - proven - fals
        w_other = a["processed"] - w.get("PROVEN", 0) - w.get("FALSIFIED", 0) - w.get("TIMEOUT", 0)
        newp = sum(1 for r in s.get("rows", []) if r.get("new_check_problems"))
        md.append(f"| {a['id']} | {a['sites']} | {a['processed']} / {a['requested']} | {a['reintegrated']} | {a['cone_synth_ok']} | "
                  f"{a['elaborates']} | {newp} | {a['full_synth_ok']}/{a['full_synth_run']} | {proven} | {fals} | {other_c} | "
                  f"{w.get('PROVEN', 0)} | {w.get('FALSIFIED', 0)} | {w.get('TIMEOUT', 0)} | {w_other} |"
                  + (f" <!-- {a['note']} -->" if a.get("note") else ""))
        for k in ("processed", "reintegrated", "cone_synth_ok", "elaborates"):
            tot[k] += a[k]
        tot["new_problems"] += newp
        tot["control_proven"] += proven
        tot["control_falsified"] += fals
        tot["wce_proven"] += w.get("PROVEN", 0)
        tot["wce_timeout"] += w.get("TIMEOUT", 0)
    md += ["", f"Total: {tot['processed']} genomes processed; {tot['reintegrated']} reintegrated; {tot['cone_synth_ok']} cone syntheses; "
               f"{tot['elaborates']} elaborate ({tot['new_problems']} with new `check` problems); control proofs PROVEN {tot['control_proven']}, "
               f"FALSIFIED {tot['control_falsified']}; WCE proofs PROVEN {tot['wce_proven']}, TIMEOUT {tot['wce_timeout']}."]
    (od / "random_genomes.md").write_text("\n".join(md) + "\n")
    return tot["control_falsified"]


def rereport(bd: Path, od: Path, proof_timeout: int, full_synth_luts: int) -> int:
    """Recompute the elaboration columns of an existing run from its stored reintegrated designs, then rewrite the report."""
    results = json.loads((od / "random_genomes.json").read_text())
    for s in results:
        d = bd / s["id"]
        if not (d / "cone_meta.json").exists():
            continue
        top = json.loads((d / "cone_meta.json").read_text())["top_module"]
        def redo(r: Dict[str, Any], top: str = top, d: Path = d, sid: str = s["id"]) -> None:
            final = od / "work" / sid / "runs" / f"final_{r['idx']:03d}.v"
            if final.exists():
                if str(r.get("error", "")).startswith("elaboration"):
                    r.pop("error")
                r.pop("new_check_problems", None)
                r.update(elaboration(final, top, str(d / "exact_baseline_top.v")))

        with ThreadPoolExecutor(max_workers=4) as pool:        # four Yosys processes at once (twelve ran the machine out of memory)
            list(pool.map(redo, s.get("rows", [])))
        print(f"{s['id']}: {len(s.get('rows', []))} designs rechecked", flush=True)
    return write_report(results, od, proof_timeout, full_synth_luts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--designs", nargs="*", default=None)
    ap.add_argument("--jobs", type=int, default=4, help="designs at once")
    ap.add_argument("--inner", type=int, default=2, help="genomes at once inside one design")
    ap.add_argument("--budget-s", type=float, default=1500.0, help="wall time per design after which no new genome starts")
    ap.add_argument("--proof-timeout", type=int, default=30)
    ap.add_argument("--full-synth-luts", type=int, default=2500)
    ap.add_argument("--rereport", action="store_true", help="recompute the elaboration columns of an existing run in --out-dir and rewrite its report")
    args = ap.parse_args()
    bd, od = Path(args.build_dir), Path(args.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    if args.rereport:
        sys.exit(1 if rereport(bd, od, args.proof_timeout, args.full_synth_luts) else 0)
    dirs = [p for p in sorted(bd.iterdir()) if (p / "cone_meta.json").exists() and (p / "result.json").exists()]
    if args.designs:
        dirs = [p for p in dirs if p.name in args.designs]
    jobs = [{"dir": str(p), "out": str(od / "work"), "n": args.n, "inner": args.inner, "budget_s": args.budget_s,
             "proof_timeout": args.proof_timeout, "full_synth_luts": args.full_synth_luts} for p in dirs]
    ctx = mp.get_context("spawn")
    results = []
    with ctx.Pool(processes=args.jobs, maxtasksperchild=1) as pool:
        for s in pool.imap_unordered(run_design, jobs):
            agg = aggregate(s)
            print(f"{agg['id']}: processed {agg['processed']}/{agg['requested']}, control {agg['control']}, wce {agg['wce']}", flush=True)
            results.append(s)
    falsified = write_report(results, od, args.proof_timeout, args.full_synth_luts)
    print(f"Wrote {od / 'random_genomes.md'}")
    sys.exit(1 if falsified else 0)


if __name__ == "__main__":
    main()
