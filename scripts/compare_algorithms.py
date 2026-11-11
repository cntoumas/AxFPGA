#!/usr/bin/env python3
"""Compare the search algorithms of the design-space exploration on a fixed set of cones.

For each design (its extracted cone and limits taken from a finished suite run), the truncation technique is explored
by every algorithm with several seeds and the same evaluation budget. Reported per run: best feasible cost, hypervolume
of the archive's Pareto front (reference: exact cost x error limit), evaluations to reach 95 % of the run's saving.
Summary: mean / std per algorithm and design, mean rank, and significance with the designs as blocks: Friedman test
over the per-design mean savings, one-sided Wilcoxon signed-rank of each algorithm against random search on the
per-design means, and per-design one-sided Mann-Whitney U against random (seeds as samples); p-values Holm-corrected
over the algorithms. Designs where every algorithm reaches the same saving (exhaustively enumerated or trivial
spaces) carry no information and are listed apart.

One task per (design, algorithm, seed); rows are appended to OUT/runs_partial.jsonl as they finish, so a resubmitted
job skips the runs already done. A failing run is recorded with its error and left out of the statistics.

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
# union of the two earlier comparisons (algorithms + ga_family)
ALL_ALGOS = ("OriginalTS", "OriginalSA", "OriginalACOR", "JADE", "BaseGA", "SingleGA", "EliteSingleGA", "MultiGA",
             "EliteMultiGA", "OriginalES", "OriginalEP", "CMA_ES", "OriginalSHADE", "L_SHADE", "SADE", "OriginalCRO",
             "OriginalMA", "random", "greedy")


def holm(p: Dict[str, float]) -> Dict[str, float]:
    """Holm-Bonferroni adjusted p-values (None entries are kept as None)."""
    items = sorted((v, k) for k, v in p.items() if v is not None)
    m, out, run = len(items), {k: None for k in p}, 0.0
    for i, (v, k) in enumerate(items):
        run = max(run, min(1.0, (m - i) * v))
        out[k] = run
    return out


def limits_of(rec: Dict[str, Any]) -> Dict[str, float]:
    out = {}
    for q in rec.get("quality") or []:
        if q["metric"] in ("wce", "med", "mred", "er", "rmse") and q["op"] in ("<=", "<"):
            out[q["metric"]] = float(q["threshold"])
    if not out and rec.get("bound") is not None:
        out["wce"] = float(rec["bound"])
    return out


def run_one(job: Dict[str, Any]) -> Dict[str, Any]:
    """One (design, algorithm, seed) run; never raises (an error is returned in the row)."""
    logging.basicConfig(level=logging.WARNING)
    t0 = time.time()
    base = {"design": job["design"], "algorithm": job["algo"], "seed": job["seed"]}
    try:
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
        s = Exploration(tech, ev, work / "run", algorithm=job["algo"], budget_fe=job["budget"],
                        budget_s=job["budget_s"], seed=job["seed"], jobs=job["jobs"], primary=primary, sweep=False).run()
        best = s["best"]["cost"] if s["best"] else ev.base_cost
        row = dict(base, base_cost=ev.base_cost, best_cost=best, saving_pct=100.0 * (ev.base_cost - best) / ev.base_cost,
                   hypervolume=s["hypervolume"], evals_to_95pct=s["evals_to_95pct"], evaluations=s["evaluations"],
                   dims=s["dims"], strategy=s.get("strategy"), space_size=s.get("space_size"),
                   time_s=round(time.time() - t0, 1), convergence=s["convergence"],
                   best_genome=s["best"]["describe"] if s["best"] else None)
    except Exception as exc:                        # recorded, not fatal: the other runs go on
        import traceback
        row = dict(base, error=f"{type(exc).__name__}: {exc}"[:500], trace=traceback.format_exc()[-1500:],
                   time_s=round(time.time() - t0, 1))
    finally:
        import shutil
        shutil.rmtree(job["work"], ignore_errors=True)   # node-local scratch: keep only the row
    return row


def summarize(rows: List[Dict[str, Any]], out: Path) -> Dict[str, Any]:
    from scipy.stats import mannwhitneyu

    errors = [r for r in rows if r.get("error")]
    rows = [r for r in rows if not r.get("error")]
    algos_all = {r["algorithm"] for r in rows}
    designs = sorted(d for d in {r["design"] for r in rows}      # partial data: only designs every algorithm ran on
                     if {r["algorithm"] for r in rows if r["design"] == d} == algos_all)
    rows = [r for r in rows if r["design"] in designs]
    algos = list(dict.fromkeys([a for a in ALGOS if any(r["algorithm"] == a for r in rows)] + sorted({r["algorithm"] for r in rows})))
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
    # designs as blocks: only designs where the algorithms differ carry information
    informative = [d for d in designs if len({round(table[(d, a)]["saving_mean"], 9) for a in algos}) > 1]
    flat = [d for d in designs if d not in informative]
    friedman_p = None
    wil, mwu_design = {}, {}
    try:
        from scipy.stats import friedmanchisquare, wilcoxon
        if len(informative) >= 2 and len(algos) >= 3:
            friedman_p = float(friedmanchisquare(*[[table[(d, a)]["saving_mean"] for d in informative] for a in algos]).pvalue)
        for a in algos:
            if a == "random":
                continue
            diff = [table[(d, a)]["saving_mean"] - table[(d, "random")]["saving_mean"] for d in informative] \
                if "random" in algos else []
            try:
                wil[a] = float(wilcoxon(diff, alternative="greater").pvalue) if any(x != 0 for x in diff) else None
            except ValueError:
                wil[a] = None
            for d in informative:
                x = [r["saving_pct"] for r in rows if r["design"] == d and r["algorithm"] == a]
                y = [r["saving_pct"] for r in rows if r["design"] == d and r["algorithm"] == "random"]
                try:
                    mwu_design[(d, a)] = float(mannwhitneyu(x, y, alternative="greater").pvalue) if x and y else None
                except ValueError:
                    mwu_design[(d, a)] = None
    except ImportError:
        pass
    wil_holm = holm(wil)
    pooled_holm = holm(pvals)
    mwu_holm = {}
    for d in informative:
        adj = holm({a: mwu_design.get((d, a)) for a in algos if a != "random"})
        for a, v in adj.items():
            mwu_holm[(d, a)] = v
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
    g = lambda v: "-" if v is None else f"{v:.3g}"
    md += ["", f"Informative designs (algorithms differ): {len(informative)} of {len(designs)}"
           + (f"; no difference on: {', '.join(flat)}" if flat else "") + ".",
           f"Friedman test over the informative designs (blocks), algorithms as treatments: p = {g(friedman_p)}.", "",
           "| Algorithm | Mean rank (1 = best) | Wilcoxon vs random, per-design means | Holm | "
           "designs better than random (per-design Mann-Whitney, Holm < 0.05) | pooled Mann-Whitney (cost) | Holm |",
           "|:--|--:|--:|--:|--:|--:|--:|"]
    for a in sorted(algos, key=lambda a: mean_rank[a]):
        nsig = sum(1 for d in informative if (mwu_holm.get((d, a)) or 1.0) < 0.05)
        md.append(f"| {a} | {mean_rank[a]:.2f} | {g(wil.get(a))} | {g(wil_holm.get(a))} | "
                  f"{'-' if a == 'random' else f'{nsig} of {len(informative)}'} | {g(pvals.get(a))} | {g(pooled_holm.get(a))} |")
    if errors:
        md += ["", f"{len(errors)} run(s) failed and are left out:", ""]
        md += [f"- {r['design']} {r['algorithm']} seed {r['seed']}: {r['error'][:160]}" for r in errors[:30]]
    md += ["", f"**Default chosen: `{winner}`** (best mean rank among the metaheuristics; ties broken by hypervolume)."]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    summary = {"winner": winner, "mean_rank": mean_rank, "p_vs_random": pvals, "p_vs_random_holm": pooled_holm,
               "friedman_p": friedman_p, "wilcoxon_vs_random": wil, "wilcoxon_vs_random_holm": wil_holm,
               "informative_designs": informative, "flat_designs": flat, "failed_runs": len(errors),
               "per_design_mwu_holm": [{"design": d, "algorithm": a, "p": v} for (d, a), v in mwu_holm.items()],
               "table": [{"design": d, "algorithm": a, **table[(d, a)]} for d in designs for a in algos]}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, len(designs), figsize=(3.4 * len(designs), 3.6), squeeze=False)
        for ax, d in zip(axes[0], designs):
            for a in algos:
                rr = [r for r in rows if r["design"] == d and r["algorithm"] == a and r["convergence"]]
                if not rr:
                    continue
                L = max(len(r["convergence"]) for r in rr)
                curves = []
                for r in rr:
                    c = [x if x is not None else r["base_cost"] for x in r["convergence"]]
                    c = c + [c[-1]] * (L - len(c)) if c else [r["base_cost"]] * L
                    curves.append([100.0 * (r["base_cost"] - v) / r["base_cost"] for v in c])
                mean = [max(0.0, statistics.mean(col)) for col in zip(*curves)]
                style = {"OriginalTS": dict(color="black", lw=2.2), "random": dict(color="grey", lw=1.4, ls="--")}.get(
                    a, dict(color=plt.cm.tab20(algos.index(a) % 20), lw=1.1))
                ax.plot(range(1, L + 1), mean, label=a, **style)
            ax.set_title(d, fontsize=9)
            ax.set_xlabel("evaluations", fontsize=8)
            ax.set_ylabel("cone cost saved (%)", fontsize=8)
            ax.grid(alpha=0.3)
        handles, labels = axes[0][0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=min(8, len(labels)), fontsize=7, frameon=False)
        fig.tight_layout(rect=(0, 0.12, 1, 1))
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
    ap.add_argument("--seed-start", type=int, default=1, help="first seed (fresh seeds: not the ones that chose the default)")
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--budget-s", type=float, default=900)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--summarize-only", action="store_true")
    ap.add_argument("--from-log", default=None, help="rebuild runs.json from the per-run lines of a log (no convergence curves)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.from_log:
        import re
        pat = re.compile(r"^(\S+)\s+(\S+)\s+seed (\d+): best (\S+) \(base (\S+)\) hv (\S+) evals (\d+) (\d+)s")
        rows = []
        for ln in Path(a.from_log).read_text().splitlines():
            m = pat.match(ln.strip())
            if not m:
                continue
            d, algo, seed, best, base, hv, ev, t = m.groups()
            best, base = float(best), float(base)
            rows.append({"design": d, "algorithm": algo, "seed": int(seed), "base_cost": base, "best_cost": best,
                         "saving_pct": 100.0 * (base - best) / base, "hypervolume": float(hv), "evals_to_95pct": None,
                         "evaluations": int(ev), "time_s": float(t), "convergence": [], "best_genome": None})
        (out / "runs.json").write_text(json.dumps(rows, indent=1))
    elif a.summarize_only:
        part = out / "runs_partial.jsonl"
        rows = (json.loads((out / "runs.json").read_text()) if (out / "runs.json").exists()
                else [json.loads(ln) for ln in part.read_text().splitlines() if ln.strip()])
    else:
        res = {r["id"]: r for r in json.loads(Path(a.run_results).read_text())}
        part = out / "runs_partial.jsonl"
        rows = [json.loads(ln) for ln in part.read_text().splitlines() if ln.strip()] if part.exists() else []
        done = {(r["design"], r["algorithm"], r["seed"]) for r in rows if not r.get("error")}
        rows = [r for r in rows if not r.get("error")]          # failed runs are retried
        seeds = list(range(a.seed_start, a.seed_start + a.seeds))
        jobs = []
        for d in a.designs.split(","):
            b = Path(a.run_build) / d
            if d not in res or not (b / "extracted_cone.v").exists():
                print(f"skip {d}: no cone or no result in the run", flush=True)
                continue
            lim = limits_of(res[d])
            for seed in seeds:                       # seed-major: an interrupted job has every algorithm at low seeds
                for algo in a.algos.split(","):
                    if (d, algo, seed) in done:
                        continue
                    jobs.append({"design": d, "cone": str(b / "extracted_cone.v"), "meta": str(b / "cone_meta.json"),
                                 "limits": lim, "work": str(Path(a.work) / d / algo / f"s{seed}"), "algo": algo,
                                 "seed": seed, "budget": a.budget, "budget_s": a.budget_s, "jobs": a.jobs})
        print(f"{len(jobs)} run(s) to do, {len(done)} already done", flush=True)
        from concurrent.futures import as_completed
        with ProcessPoolExecutor(max_workers=a.parallel) as pool, open(part, "a") as fh:
            futs = [pool.submit(run_one, j) for j in jobs]
            for f in as_completed(futs):
                r = f.result()
                fh.write(json.dumps(r) + "\n")
                fh.flush()
                rows.append(r)
                tail = r.get("error") or (f"best {r['best_cost']} (base {r['base_cost']}) hv {r['hypervolume']:.4f} "
                                          f"evals {r['evaluations']} {r['strategy']}")
                print(f"{r['design']:20} {r['algorithm']:14} seed {r['seed']}: {tail} {r['time_s']:.0f}s", flush=True)
        (out / "runs.json").write_text(json.dumps(rows, indent=1))
    s = summarize(rows, out)
    print("winner:", s["winner"])


if __name__ == "__main__":
    main()
