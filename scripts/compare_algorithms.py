#!/usr/bin/env python3
"""Compare the search algorithms of the design-space exploration on a fixed set of cones.

For each design (its extracted cone and limits taken from a finished suite run), the truncation technique is explored
by every algorithm with several seeds and the same evaluation budget. Reported per run: best feasible cost, hypervolume
of the archive's Pareto front (reference: exact cost x error limit), evaluations to reach 95 % of the run's saving.
Summary: mean / std per algorithm and design, mean rank, one-sided Mann-Whitney U of each algorithm against random
search (best cost, lower is better), and the recommended default.

    python scripts/compare_algorithms.py --run-build build/final --run-results reports/final/results.json \
        --out reports/explore/algorithms
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DESIGNS = ("image_pipeline", "kinematics_fwd_bhv", "fir_filter", "multiplier_16b")
ALGOS = ("EliteSingleGA", "OriginalSA", "OriginalTS", "JADE", "OriginalACOR", "random", "greedy")


def limits_of(rec: Dict[str, Any]) -> Dict[str, float]:
    out = {}
    for q in rec.get("quality") or []:
        if q["metric"] in ("wce", "med", "mred", "er", "rmse") and q["op"] in ("<=", "<"):
            out[q["metric"]] = float(q["threshold"])
    if not out and rec.get("bound") is not None:
        out["wce"] = float(rec["bound"])
    return out


def run_design(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    logging.basicConfig(level=logging.WARNING)
    from axfpga.estimator import FpgaEstimator
    from axfpga.explore.evaluate import ConeEvaluator
    from axfpga.explore.search import Exploration
    from axfpga.explore.stage import list_cone_sites
    from axfpga.explore.techniques import SiteOpsTechnique

    work = Path(job["work"])
    est = FpgaEstimator(family="xc7", device="xc7z010")
    meta = Path(job["meta"]) if job["meta"] and Path(job["meta"]).exists() else None
    ev = ConeEvaluator(Path(job["cone"]), meta, work / "eval", est, job["limits"])
    sites = list_cone_sites(ev.exact_v, ev.top, ev.rel, work / "sites")
    tech = SiteOpsTechnique("trunc", sites, ev.exact_v, ev.top, ev.rel)
    primary = "med" if "wce" not in job["limits"] else "wce"
    rows = []
    for algo in job["algos"]:
        for seed in job["seeds"]:
            t0 = time.time()
            s = Exploration(tech, ev, work / f"{algo}_s{seed}", algorithm=algo, budget_fe=job["budget"],
                            budget_s=job["budget_s"], seed=seed, jobs=job["jobs"], primary=primary, sweep=False).run()
            best = s["best"]["cost"] if s["best"] else ev.base_cost
            rows.append({"design": job["design"], "algorithm": algo, "seed": seed, "base_cost": ev.base_cost,
                         "best_cost": best, "saving_pct": 100.0 * (ev.base_cost - best) / ev.base_cost,
                         "hypervolume": s["hypervolume"], "evals_to_95pct": s["evals_to_95pct"],
                         "evaluations": s["evaluations"], "dims": s["dims"], "time_s": round(time.time() - t0, 1),
                         "convergence": s["convergence"], "best_genome": s["best"]["describe"] if s["best"] else None})
            print(f"{job['design']:20} {algo:14} seed {seed}: best {best} (base {ev.base_cost}) hv {s['hypervolume']:.4f} "
                  f"evals {s['evaluations']} {time.time() - t0:.0f}s", flush=True)
    return rows


def summarize(rows: List[Dict[str, Any]], out: Path) -> Dict[str, Any]:
    from scipy.stats import mannwhitneyu

    designs = sorted({r["design"] for r in rows})
    algos = [a for a in ALGOS if any(r["algorithm"] == a for r in rows)]
    table = {}
    ranks: Dict[str, List[float]] = {a: [] for a in algos}
    for d in designs:
        means = {}
        for a in algos:
            rr = [r for r in rows if r["design"] == d and r["algorithm"] == a]
            sv = [r["saving_pct"] for r in rr]
            hv = [r["hypervolume"] for r in rr]
            e95 = [r["evals_to_95pct"] for r in rr if r["evals_to_95pct"]]
            table[(d, a)] = {"saving_mean": statistics.mean(sv), "saving_std": statistics.pstdev(sv),
                             "hv_mean": statistics.mean(hv), "e95_mean": statistics.mean(e95) if e95 else None,
                             "best": max(sv)}
            means[a] = (table[(d, a)]["saving_mean"], table[(d, a)]["hv_mean"])
        order = sorted(algos, key=lambda a: (-means[a][0], -means[a][1]))
        for i, a in enumerate(order):
            tied = [b for b in order if means[b] == means[a]]
            ranks[a].append(statistics.mean(order.index(b) + 1 for b in tied))
    pvals = {}
    for a in algos:
        if a == "random":
            continue
        x = [r["best_cost"] / r["base_cost"] for r in rows if r["algorithm"] == a]
        y = [r["best_cost"] / r["base_cost"] for r in rows if r["algorithm"] == "random"]
        try:
            pvals[a] = float(mannwhitneyu(x, y, alternative="less").pvalue) if x and y else None
        except ValueError:
            pvals[a] = None
    mean_rank = {a: statistics.mean(ranks[a]) for a in algos}
    meta_algos = [a for a in algos if a not in ("random", "greedy")]
    winner = min(meta_algos, key=lambda a: (mean_rank[a], -statistics.mean(table[(d, a)]["hv_mean"] for d in designs)))
    md = ["# Search-algorithm comparison", "",
          f"Technique: truncation (operands, named wires, output bus) on the extracted cone. Budget {rows[0]['evaluations'] if rows else 0}-ish "
          "evaluations per run; seeds per algorithm as listed in runs.json. Saving = cone cost (LC + 20 x DSP48) saved by the best "
          "feasible point. HV = hypervolume of the cost/error Pareto front (reference exact cost x error limit, normalized).", "",
          "| Design | Algorithm | Saving % mean | std | best | HV mean | evals to 95 % |", "|:--|:--|--:|--:|--:|--:|--:|"]
    for d in designs:
        for a in algos:
            t = table[(d, a)]
            e95 = "-" if t["e95_mean"] is None else f"{t['e95_mean']:.0f}"
            md.append(f"| {d} | {a} | {t['saving_mean']:.1f} | {t['saving_std']:.1f} | {t['best']:.1f} | {t['hv_mean']:.4f} | {e95} |")
    md += ["", "| Algorithm | Mean rank (1 = best) | p vs random (one-sided Mann-Whitney, cost) |", "|:--|--:|--:|"]
    for a in sorted(algos, key=lambda a: mean_rank[a]):
        p = pvals.get(a)
        md.append(f"| {a} | {mean_rank[a]:.2f} | {'-' if p is None else f'{p:.3g}'} |")
    md += ["", f"**Default chosen: `{winner}`** (best mean rank among the metaheuristics; ties broken by hypervolume)."]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    summary = {"winner": winner, "mean_rank": mean_rank, "p_vs_random": pvals,
               "table": [{"design": d, "algorithm": a, **table[(d, a)]} for d in designs for a in algos]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, len(designs), figsize=(3.4 * len(designs), 3.0), squeeze=False)
        for ax, d in zip(axes[0], designs):
            for a in algos:
                rr = [r for r in rows if r["design"] == d and r["algorithm"] == a]
                L = max(len(r["convergence"]) for r in rr)
                curves = []
                for r in rr:
                    c = [x if x is not None else r["base_cost"] for x in r["convergence"]]
                    c = c + [c[-1]] * (L - len(c)) if c else [r["base_cost"]] * L
                    curves.append([100.0 * (r["base_cost"] - v) / r["base_cost"] for v in c])
                mean = [statistics.mean(col) for col in zip(*curves)]
                ax.plot(range(1, L + 1), mean, label=a, lw=1.2)
            ax.set_title(d, fontsize=9)
            ax.set_xlabel("evaluations", fontsize=8)
            ax.set_ylabel("cone cost saved (%)", fontsize=8)
            ax.grid(alpha=0.3)
        axes[0][0].legend(fontsize=6, frameon=False)
        fig.tight_layout()
        fig.savefig(out / "convergence.png", dpi=150)
    except Exception as exc:
        print("plot failed:", exc)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-build", default="build/final")
    ap.add_argument("--run-results", default="reports/final/results.json")
    ap.add_argument("--out", default="reports/explore/algorithms")
    ap.add_argument("--work", default="build/explore_algos")
    ap.add_argument("--designs", default=",".join(DESIGNS))
    ap.add_argument("--algos", default=",".join(ALGOS))
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--budget-s", type=float, default=900)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--summarize-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.summarize_only:
        rows = json.loads((out / "runs.json").read_text())
    else:
        res = {r["id"]: r for r in json.loads(Path(a.run_results).read_text())}
        jobs = []
        for d in a.designs.split(","):
            b = Path(a.run_build) / d
            jobs.append({"design": d, "cone": str(b / "extracted_cone.v"), "meta": str(b / "cone_meta.json"),
                         "limits": limits_of(res[d]), "work": str(Path(a.work) / d), "algos": a.algos.split(","),
                         "seeds": list(range(1, a.seeds + 1)), "budget": a.budget, "budget_s": a.budget_s, "jobs": a.jobs})
        rows = []
        with ProcessPoolExecutor(max_workers=a.parallel) as pool:
            for r in pool.map(run_design, jobs):
                rows.extend(r)
        (out / "runs.json").write_text(json.dumps(rows, indent=1))
    s = summarize(rows, out)
    print("winner:", s["winner"])


if __name__ == "__main__":
    main()
