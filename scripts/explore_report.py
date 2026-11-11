#!/usr/bin/env python3
"""Supervisor report of a design-space-exploration suite run (AXFPGA_SEARCH=meta).

    python scripts/explore_report.py --run reports/explore --baseline reports/final \
        --algos reports/explore/algorithms --out reports/explore

Writes REPORT.md, report_data.json and figures/*.png into --out (then scripts/report_html.py makes REPORT.html).
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import final_report as fr  # noqa: E402
from final_report import APPROX, BAD, EXACT, GOOD, GRID, INK, INK2, WARN, f, md_table, short  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

TECH_COLORS = {"trunc": "#1f6feb", "loa": "#2a9d6f", "nibble": "#8250df", "ppg": "#d9962b", "pyals_lut": "#c8453b",
               "loa_cone": "#5fb98f", "ppg_cone": "#e8b45a", "compressors": "#6e7781", "nibble_cone": "#a98be0",
               "pyals_prune": "#e0776e", "out_prune": "#57606a", "pyals_catalog": "#f0a39b", "ppg_design": "#b07405",
               "trunc_reg": "#0a3069"}
PRIMS = [("LUT1", "LUT1"), ("LUT2", "LUT2"), ("LUT3", "LUT3"), ("LUT4", "LUT4"), ("LUT5", "LUT5"), ("LUT6", "LUT6"),
         ("FD", "FF (FD*)"), ("CARRY4", "CARRY4"), ("DSP48E1", "DSP48E1"), ("MUXF7", "MUXF7"), ("MUXF8", "MUXF8"),
         ("SRL", "SRL"), ("RAMB18E1", "RAMB18"), ("RAMB36E1", "RAMB36")]


def prim_count(p: Dict[str, int], key: str) -> int:
    if key in ("FD", "SRL"):
        return sum(v for k, v in p.items() if k.startswith(key))
    return int(p.get(key, 0))


def fig_pareto(results: List[Dict[str, Any]], out: Path) -> Optional[str]:
    recs = [r for r in results if r.get("exploration") and any(t.get("front") for t in r["exploration"]["techniques"])]
    if not recs:
        return None
    n = len(recs)
    cols = 4
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(3.0 * cols, 2.5 * rows), squeeze=False)
    used = set()
    for ax, r in zip(axes.flat, recs):
        x = r["exploration"]
        base = x["techniques"][0]["base_cost"] if x["techniques"] else 1
        prim = x["primary_metric"]
        lim = (x.get("limits") or {}).get(prim) or 1.0
        for t in x["techniques"]:
            pts = [(p["errors"].get(prim, 0) / lim, 100.0 * (t["base_cost"] - p["cost"]) / t["base_cost"])
                   for p in t.get("front") or [] if p.get("cost") is not None and p.get("errors")]
            if not pts:
                continue
            pts.sort()
            c = TECH_COLORS.get(t["technique"], INK2)
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", ms=2.5, lw=1, color=c)
            used.add(t["technique"])
        ax.axvline(1.0, color=INK2, lw=0.6, ls="--")
        ax.set_title(short(r["id"]), fontsize=8.5)
        ax.tick_params(labelsize=7)
        ax.grid(color=GRID, lw=0.5)
    for ax in list(axes.flat)[n:]:
        ax.axis("off")
    for ax in axes[-1]:
        ax.set_xlabel("error / limit", fontsize=7.5)
    for ax in axes[:, 0]:
        ax.set_ylabel("cone cost saved %", fontsize=7.5)
    handles = [plt.Line2D([0], [0], color=TECH_COLORS.get(t, INK2), lw=2) for t in sorted(used)]
    fig.legend(handles, sorted(used), loc="lower center", ncol=min(7, len(used)), fontsize=7, frameon=False)
    fig.suptitle("Pareto fronts per technique (archive of the search, cone level)", fontsize=10, x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    fig.savefig(out / "pareto.png")
    plt.close(fig)
    return "pareto.png"


def fig_wins(results: List[Dict[str, Any]], out: Path) -> Optional[str]:
    wins: Dict[str, int] = {}
    feas: Dict[str, int] = {}
    for r in results:
        x = r.get("exploration")
        if not x:
            continue
        if x.get("winner"):
            wins[x["winner"]["technique"]] = wins.get(x["winner"]["technique"], 0) + 1
        for t in x["techniques"]:
            if t.get("check", {}).get("passed"):
                feas[t["technique"]] = feas.get(t["technique"], 0) + 1
    keys = sorted(set(wins) | set(feas), key=lambda k: (-wins.get(k, 0), -feas.get(k, 0), k))
    if not keys:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 0.32 * len(keys) + 1.2))
    y = np.arange(len(keys))
    ax.barh(y + 0.2, [feas.get(k, 0) for k in keys], 0.4, color=EXACT, label="designs where the technique produced a passing design")
    ax.barh(y - 0.2, [wins.get(k, 0) for k in keys], 0.4, color=APPROX, label="designs where it won")
    ax.set_yticks(y, keys)
    ax.invert_yaxis()
    ax.set_xlabel("designs")
    ax.set_title("Which technique wins", loc="left", fontsize=10.5, fontweight="bold")
    ax.legend(frameon=False, fontsize=7.5, loc="lower right")
    ax.grid(axis="x", color=GRID, lw=0.6)
    fig.tight_layout()
    fig.savefig(out / "technique_wins.png")
    plt.close(fig)
    return "technique_wins.png"


def fig_vs_baseline(rows: List[Dict[str, Any]], base: Dict[str, Dict[str, Any]], out: Path) -> Optional[str]:
    common = [r for r in rows if r["id"] in base]
    if not common:
        return None
    common.sort(key=lambda r: -r["lc_saved"])
    fig, ax = plt.subplots(figsize=(7.2, 0.32 * len(common) + 1.3))
    y = np.arange(len(common))
    ax.barh(y + 0.2, [base[r["id"]]["savings_pct"]["logic_cells"] for r in common], 0.4, color=EXACT, label="fixed candidate menu (Tournament)")
    ax.barh(y - 0.2, [r["lc_saved"] for r in common], 0.4, color=APPROX, label="design-space exploration")
    ax.set_yticks(y, [short(r["id"]) for r in common])
    ax.invert_yaxis()
    ax.set_xlabel("logic cells saved vs exact (%)")
    ax.set_title("Exploration vs the previous fixed menu", loc="left", fontsize=10.5, fontweight="bold")
    ax.legend(frameon=False, fontsize=7.5, loc="lower right")
    ax.grid(axis="x", color=GRID, lw=0.6)
    fig.tight_layout()
    fig.savefig(out / "vs_tournament.png")
    plt.close(fig)
    return "vs_tournament.png"


def build(run: Path, out: Path, baseline: Optional[Path], algos: Optional[Path], ga_family: Optional[Path] = None) -> None:
    results = json.loads((run / "results.json").read_text())
    rows = [fr.flatten(r) for r in results]
    byid = {r["id"]: r for r in results}
    for row in rows:
        r = byid[row["id"]]
        row["errors_full"] = r.get("errors_full") or {}
        row["prim_exact"] = r["exact"].get("primitives") or {}
        row["prim_approx"] = r["approx"].get("primitives") or {}
        row["pb_exact"] = r["exact"].get("power_breakdown") or {}
        row["pb_approx"] = r["approx"].get("power_breakdown") or {}
        row["exact_rewrite"] = bool(r.get("exact_rewrite"))
    rows.sort(key=lambda r: (-r["lc_saved"], r["id"]))
    figs = out / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    approx = [r for r in rows if not r["fell_back"]]
    saved = [r["lc_saved"] for r in approx]
    base = {}
    if baseline and (baseline / "results.json").exists():
        base = {r["id"]: r for r in json.loads((baseline / "results.json").read_text())}
    base_mean_all = statistics.mean(b["savings_pct"]["logic_cells"] for b in base.values()) if base else None
    summary = {
        "designs": len(rows), "approximated": len(approx), "kept_exact": len(rows) - len(approx),
        "mean_lc_saved_all": statistics.mean(r["lc_saved"] for r in rows) if rows else 0.0,
        "mean_lc_saved_approx": statistics.mean(saved) if saved else 0.0,
        "median_lc_saved_approx": statistics.median(saved) if saved else 0.0,
        "max_lc_saved": max(saved) if saved else 0.0,
        "max_lc_design": max(approx, key=lambda r: r["lc_saved"])["id"] if approx else "",
        "mean_power_saved_approx": statistics.mean(r["p_saved"] for r in approx) if approx else 0.0,
        "app_ok": sum(1 for r in approx if r["app_pass"]),
        "exact_rewrites": [r["id"] for r in approx if r["exact_rewrite"]],
        "mean_lc_saved_lossy": (statistics.mean(r["lc_saved"] for r in approx if not r["exact_rewrite"])
                                if any(not r["exact_rewrite"] for r in approx) else 0.0),
        "baseline_mean_all": base_mean_all,
        "lc_exact_total": sum(r["lc_exact"] for r in rows), "lc_approx_total": sum(r["lc_approx"] for r in rows),
    }
    (out / "report_data.json").write_text(json.dumps({"summary": summary, "rows": [{k: v for k, v in r.items() if k != "quality"} for r in rows]}, indent=1, default=str))

    fr.barh_savings(approx, "lc_saved", "Logic cells saved per design", "savings_lc.png", figs)
    fr.barh_savings(approx, "p_saved", "Dynamic power saved per design (indicative)", "savings_power.png", figs, color="#8250df")
    fr.fig_lc(rows, figs)
    fr.fig_power(rows, figs)
    fr.fig_error_use(rows, figs)
    fr.fig_util(approx, figs)
    f_par = fig_pareto(results, figs)
    f_win = fig_wins(results, figs)
    f_vs = fig_vs_baseline(rows, base, figs)
    algo_md, algo_fig = None, None
    if algos and (algos / "summary.md").exists():
        algo_md = (algos / "summary.md").read_text()
        if (algos / "convergence.png").exists():
            shutil.copy(algos / "convergence.png", figs / "algorithms_convergence.png")
            algo_fig = "algorithms_convergence.png"

    md: List[str] = []
    md += ["# AxFPGA: design-space exploration results", "",
           "Target: Zybo Z7-10 (xc7z010clg400-1; the 89-multiplier designs on xc7z020). Synthesis: Yosys `synth_xilinx -family xc7`, "
           "DSP48 inference on. Cost: logic cells (LC = LUT + 2 x CARRY4) + 20 x DSP48. Power: activity-based Yosys estimate over "
           "256 cycles, indicative only.", "",
           "**Method.** The designer annotates the relaxable output and the precise control and gives the error limits. The tool "
           "extracts the safe cone C_safe and, for every approximation technique, searches it: which wire, register, adder, "
           "multiplier or LUT to approximate and how deep. Per-site spaces are searched with a metaheuristic (mealpy); small "
           "spaces are enumerated exhaustively; the array-multiplier depth is bisected. The best points of every technique are "
           "synthesized, checked on the application stimulus, and the cheapest passing design wins.", "",
           "## Headline", "",
           md_table(["Measure", "Value"], [
               ["Designs evaluated", str(summary["designs"])],
               ["Approximated / kept exact", f"{summary['approximated']} / {summary['kept_exact']}"],
               ["Mean logic cells saved (all designs)", f"{summary['mean_lc_saved_all']:.1f} %"
                + (f" (fixed menu before: {base_mean_all:.1f} %)" if base_mean_all is not None else "")],
               ["Mean logic cells saved (approximated designs)", f"{summary['mean_lc_saved_approx']:.1f} % (median {summary['median_lc_saved_approx']:.1f} %)"],
               ["Of which exact rewrites (zero error on application and held-out stream)",
                f"{len(summary['exact_rewrites'])}" + (f" ({', '.join(summary['exact_rewrites'])})" if summary['exact_rewrites'] else "")],
               ["Mean logic cells saved (lossy approximations only)", f"{summary['mean_lc_saved_lossy']:.1f} %"],
               ["Best design", f"{summary['max_lc_design']} at {summary['max_lc_saved']:.1f} %"],
               ["Mean dynamic power saved (approximated designs)", f"{summary['mean_power_saved_approx']:.1f} %"],
               ["Application metric met", f"{summary['app_ok']} of {summary['approximated']}"],
               ["Total logic cells", f"{summary['lc_exact_total']:,} -> {summary['lc_approx_total']:,}"],
           ]), "", "![Logic cells saved](figures/savings_lc.png)", ""]
    if f_vs:
        md += ["![Exploration vs fixed menu](figures/vs_tournament.png)", ""]

    md += ["## 1. Savings and resources", ""]
    body = [[f"**{r['id']}**" + (" (exact rewrite)" if r["exact_rewrite"] else ""), r["domain"], f"{r['lc_exact']} -> {r['lc_approx']}", f"**{r['lc_saved']:.1f}**",
             f"{r['lut_exact']} -> {r['lut_approx']}", f"{r['ff_exact']} -> {r['ff_approx']}",
             f"{r['dsp_exact']} -> {r['dsp_approx']}", f"{r['carry_exact']} -> {r['carry_approx']}",
             f"{r['bram_exact']} -> {r['bram_approx']}"] for r in rows]
    md += [md_table(["Design", "Domain", "Logic cells", "Saved %", "LUT", "FF", "DSP48", "CARRY4", "BRAM"], body, left=2), "",
           "![Logic cells](figures/logic_cells.png)", "", "![Resources saved](figures/utilization.png)", "",
           "### FPGA primitives (exact -> approximate)", ""]
    present = [(k, lab) for k, lab in PRIMS if any(prim_count(r["prim_exact"], k) or prim_count(r["prim_approx"], k) for r in rows)]
    body = []
    for r in rows:
        body.append([f"**{r['id']}**"] + [
            (lambda e, a: "-" if e == a == 0 else (str(e) if e == a else f"{e}->{a}"))(prim_count(r["prim_exact"], k), prim_count(r["prim_approx"], k))
            for k, _ in present])
    md += [md_table(["Design"] + [lab for _, lab in present], body), "",
           "## 2. Power (indicative)", ""]
    classes = sorted({c for r in rows for c in list(r["pb_exact"]) + list(r["pb_approx"])})
    body = []
    for r in rows:
        cells = [f"**{r['id']}**", f"{r['p_exact']:.2f}", f"{r['p_approx']:.2f}", f"{r['p_saved']:.1f}"]
        for c in classes:
            e = (r["pb_exact"].get(c) or {}).get("power_mw", 0.0)
            a = (r["pb_approx"].get(c) or {}).get("power_mw", 0.0)
            cells.append("-" if e == a == 0 else f"{e:.2f}->{a:.2f}")
        body.append(cells)
    md += [md_table(["Design", "Exact mW", "Approx mW", "Saved %"] + [f"{c} mW" for c in classes], body), "",
           "![Power](figures/power.png)", "", "![Power saved](figures/savings_power.png)", "",
           "## 3. Error metrics", "",
           "Measured on the application stimulus, relaxed output decoded in its number format (LSB units). "
           "ER error rate, MED mean error distance, NMED = MED / full scale, MRED mean relative error, WCE worst-case error "
           "(also as % of full scale), WCRE worst-case relative error, RMSE, bias = mean signed error (approx - exact).", ""]
    body = []
    for r in approx:
        e = r["errors_full"]
        body.append([f"**{r['id']}**", f(e.get("er", 0) * 100, 1), f(e.get("med"), 3), f(e.get("nmed", 0) * 100, 4),
                     f(e.get("mred", 0) * 100, 3), f(e.get("wce"), 0), f(r["lim_wce"], 0), f(e.get("wce_pct"), 3),
                     f(e.get("wcre", 0) * 100, 2), f(e.get("rmse"), 3), f(e.get("bias"), 3)])
    md += [md_table(["Design", "ER %", "MED", "NMED %", "MRED %", "WCE", "WCE limit", "WCE % FS", "WCRE %", "RMSE", "Bias"], body), "",
           "![Error budget](figures/error_budget.png)", "",
           "## 4. Application metrics", ""]
    body = []
    for r in approx:
        v = r["app_value"]
        vs = "exact match" if v is None else f"{v:.3f} {r['app_unit']}"
        body.append([f"**{r['id']}**", r["app_name"], vs, r["app_thr"] or "none",
                     "pass" if r["app_pass"] else ("n/a" if r["app_pass"] is None else "FAIL")])
    md += [md_table(["Design", "Metric", "Value", "Required", "Result"], body, left=2), "",
           "## 5. Design-space exploration", ""]
    if f_win:
        md += ["![Technique wins](figures/technique_wins.png)", ""]
    if f_par:
        md += ["![Pareto fronts](figures/pareto.png)", ""]
    for r in results:
        x = r.get("exploration")
        if not x:
            continue
        w = x.get("winner")
        md += [f"### {r['id']}", "",
               f"Winner: **{w['technique']}**: `{w['candidate'].split(': ', 1)[-1][:120]}`" if w else
               f"No technique produced a passing design ({r.get('fallback_reason') or 'exact design kept'}).", "",
               f"Algorithm `{x['algorithm']}`; limits {', '.join(f'{k} <= {v:g}' for k, v in (x.get('limits') or {}).items())}"
               + ("; re-searched at half the limits" if x.get("re_search") else ""), ""]
        body = []
        for t in x["techniques"]:
            b = t.get("best")
            syn = t.get("synthesized")
            chk = t.get("check") or {}
            space = t["space_size"]
            space_s = f"{space:.2e}" if isinstance(space, float) or (isinstance(space, int) and space > 10 ** 6) else str(space)
            body.append([t["technique"] + (" (half)" if t.get("technique_run") else ""), t["strategy"], space_s, str(t["evaluations"]),
                         f"{b['cost']} / {t['base_cost']}" if b else f"- / {t['base_cost']}",
                         (b["describe"][:60] if b else "no feasible point"),
                         str(syn["lcs"]) if syn else "-",
                         "pass" if chk.get("passed") else ("fail: " + "; ".join(chk.get("reasons", [])[:1])[:50] if chk.get("tries") else "-")])
        md += [md_table(["Technique", "Search", "Space", "Evals", "Best cone cost / exact", "Best point", "Synth LC", "Design check"], body, left=2), ""]
        win_t = next((t for t in x["techniques"] if w and t["technique"] == w["technique"]), None)
        if not (win_t or {}).get("explanation"):
            # whole-cone winner: explain the best per-site technique instead
            site_ts = [t for t in x["techniques"] if t.get("explanation") and t.get("best")]
            win_t = min(site_ts, key=lambda t: t["best"]["cost"]) if site_ts else win_t
        expl = (win_t or {}).get("explanation")
        if expl and expl.get("sites"):
            sites = [s for s in expl["sites"] if s.get("k_in_best") or s.get("efficiency")][:8]
            if sites:
                md += [f"Why these sites ({win_t['technique']}, metric {expl['primary'].upper()}, limit {expl['limit']}):", ""]
                body = [[s["site"][:48], s["op"], str(s["max_k"]), str(s["k_in_best"]),
                         f(s.get("lc_saved_per_k"), 2), f(s.get("error_per_k"), 3), f(s.get("efficiency"), 1),
                         f"{s['in_top_pct']:.0f}%"] for s in sites]
                md += [md_table(["Site", "Operator", "Max k", "k chosen", "LC saved / bit (alone)", "Error / bit (alone)",
                                 "LC saved per % of budget", "In best 10%"], body, left=2), ""]
                md += [f"- {line}" for line in expl.get("why", [])] + [""]
    if algo_md:
        md += ["## 6. Search-algorithm comparison", "", algo_md.replace("# Search-algorithm comparison", "").strip(), ""]
        if algo_fig:
            md += [f"![Convergence](figures/{algo_fig})", ""]
    if ga_family and (ga_family / "summary.md").exists():
        md += ["### 6b. Evolutionary and genetic algorithms of mealpy", "",
               "Same protocol (4 designs, truncation technique, 5 seeds, 200 evaluations), with the GA variants (BaseGA, SingleGA, "
               "MultiGA, EliteSingleGA, EliteMultiGA), the memetic algorithm (MA), evolution strategies (ES, CMA-ES), evolutionary "
               "programming (EP), coral-reef optimization (CRO) and adaptive differential evolution (SHADE, L-SHADE, SADE), "
               "against tabu search and random search.", "",
               (ga_family / "summary.md").read_text().replace("# Search-algorithm comparison", "").strip(), ""]
        if (ga_family / "convergence.png").exists():
            shutil.copy(ga_family / "convergence.png", figs / "ga_family_convergence.png")
            md += ["![Convergence, evolutionary family](figures/ga_family_convergence.png)", ""]
    if base:
        md += ["## 7. Comparison with the fixed candidate menu (previous Tournament run)", ""]
        body = []
        for r in rows:
            b = base.get(r["id"])
            if b:
                body.append([f"**{r['id']}**", f"{b['savings_pct']['logic_cells']:.1f}", f"{r['lc_saved']:.1f}",
                             f"{r['lc_saved'] - b['savings_pct']['logic_cells']:+.1f}", b["method"][:40], r["method"][:60]])
        md += [md_table(["Design", "Fixed menu LC saved %", "Exploration LC saved %", "Difference (pp)", "Fixed-menu method", "Exploration method"], body, left=1), ""]
    (out / "REPORT.md").write_text("\n".join(md) + "\n")
    print(f"Wrote {out / 'REPORT.md'} ({len(rows)} designs)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--algos", default=None)
    ap.add_argument("--ga-family", default=None, help="second comparison: mealpy evolutionary / genetic family")
    a = ap.parse_args()
    build(Path(a.run), Path(a.out), Path(a.baseline) if a.baseline else None, Path(a.algos) if a.algos else None,
          Path(a.ga_family) if a.ga_family else None)
