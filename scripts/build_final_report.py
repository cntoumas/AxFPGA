#!/usr/bin/env python3
"""Final consolidated report of an AxBench cluster run (DSP inference on and off).

Reads <run>/dsp_on and <run>/dsp_off (results.json, run_stats.json), regenerates every figure, and writes
<run>/FINAL_REPORT.md plus <run>/figures_final/*.png. Nothing is written outside <run>.

    python scripts/build_final_report.py --run-dir reports_cluster_<tag> [--build-dir build/cluster_<tag>]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_report_figures as gf  # noqa: E402

BASE, APPROX, OK, BAD = gf.BASE, gf.APPROX, gf.OK, gf.BAD
INK2, GRID = gf.INK2, gf.GRID


def load(d: Path):
    p = d / "results.json"
    return json.loads(p.read_text()) if p.exists() else []


def num(v, nd=3):
    return "n/a" if v is None else (f"{v:.{nd}g}" if isinstance(v, float) else str(v))


def pct(v, nd=4):
    return "n/a" if v is None else f"{v:.{nd}g} %"


def runtime_fig(build: Path, figs: Path, mode: str):
    rows = []
    for sp in sorted(build.glob("*/stats.json")):
        st = json.loads(sp.read_text())
        rows.append((st["id"], st["wall_s"] / 60.0, st.get("peak_child_rss_mb", 0) / 1024.0))
    if not rows:
        return None
    rows.sort(key=lambda r: -r[1])
    fig, axes = plt.subplots(1, 2, figsize=(10, 0.36 * len(rows) + 1.6), dpi=200, sharey=True)
    y = np.arange(len(rows))
    axes[0].barh(y, [r[1] for r in rows], color=BASE)
    axes[0].set_xlabel("wall time (minutes)")
    axes[1].barh(y, [r[2] for r in rows], color=APPROX)
    axes[1].set_xlabel("peak child RSS (GB)")
    axes[0].set_yticks(y)
    axes[0].set_yticklabels([r[0] for r in rows])
    axes[0].invert_yaxis()
    for ax in axes:
        ax.grid(axis="x", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
    fig.suptitle(f"Cost per benchmark (DSP {mode})", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    out = figs / f"runtime_{mode}.png"
    fig.savefig(out)
    plt.close(fig)
    return out.name


def dsp_comparison_fig(on, off, figs: Path):
    by_off = {r["id"]: r for r in off}
    rows = [(r["name"], r["savings_pct"]["logic_cells"], by_off[r["id"]]["savings_pct"]["logic_cells"],
             r["savings_pct"]["dynamic_power_mw"], by_off[r["id"]]["savings_pct"]["dynamic_power_mw"])
            for r in on if r["id"] in by_off]
    if not rows:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(10, 0.4 * len(rows) + 1.6), dpi=200, sharey=True)
    y = np.arange(len(rows))
    h = 0.38
    for ax, (i, title) in zip(axes, ((1, "Logic-cell savings (%)"), (3, "Dynamic-power savings (%)"))):
        ax.barh(y - h / 2, [r[i] for r in rows], h, color=BASE, label="DSP inference on", edgecolor=gf.SURFACE)
        ax.barh(y + h / 2, [r[i + 1] for r in rows], h, color=APPROX, label="DSP inference off", edgecolor=gf.SURFACE)
        ax.set_xlabel(title)
        ax.grid(axis="x", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels([r[0] for r in rows])
    axes[0].invert_yaxis()
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.02), ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(figs / "dsp_comparison.png")
    plt.close(fig)
    return "dsp_comparison.png"


def wce_table(results):
    out = ["| Benchmark | Output bits | WCE (LSB) | **WCE % of full scale** | Allowed WCE (LSB) | WCE % of allowed | MED | MRED % | ER | Formal WCE |",
           "|:--|--:|--:|--:|--:|--:|--:|--:|--:|:--|"]
    for r in results:
        e, lim = r["errors"], r["limits"]
        wl = 100.0 * e["wce"] / lim["wce"] if lim.get("wce") else None
        out.append(f"| {r['name']} | {e.get('out_bits', 'n/a')} | {e['wce']:.0f} | **{pct(e.get('wce_pct'), 5)}** | {num(lim.get('wce'), 6)} | "
                   f"{'n/a' if wl is None else f'{wl:.2f} %'} | {e['med']:.3f} | {e['mred_pct']:.4f} | {e['er']:.3f} | {r['formal']['wce_bound']} |")
    return out


def section(mode: str, results, run_stats, figs_rel: str, extra_fig):
    md = [f"## DSP inference {mode}", ""]
    if run_stats:
        md += [f"Wall time {run_stats['wall_s'] / 60:.1f} min, {run_stats['jobs']} benchmarks in parallel x {run_stats['inner_jobs']} inner threads, "
               f"peak RSS {num(run_stats.get('peak_rss_gb'), 3)} GB, cache {run_stats['cache_hits']} hit / {run_stats['cache_misses']} miss.", ""]
    md += ["### Worst-case error", "", *wce_table(results), "",
           f"![WCE percent]({figs_rel}/wce_percent.png)", "",
           f"![Error budget]({figs_rel}/error_vs_bound.png)", "",
           f"![Accuracy vs area]({figs_rel}/wce_vs_savings.png)", ""]
    md += ["### Area and power", "",
           "| Benchmark | Champion | LC exact | LC approx | LC saved | Power exact (mW) | Power approx (mW) | Power saved | Certification |",
           "|:--|:--|--:|--:|--:|--:|--:|--:|:--|"]
    for r in results:
        e, a, s = r["exact"], r["approx"], r["savings_pct"]
        cert = (r.get("certification") or {}).get("tier") or r.get("certification") or ""
        md.append(f"| {r['name']} | `{r['method']}` | {e['logic_cells']} | {a['logic_cells']} | {s['logic_cells']:.1f}% | "
                  f"{e['dynamic_power_mw']:.3f} | {a['dynamic_power_mw']:.3f} | {s['dynamic_power_mw']:.1f}% | {cert if isinstance(cert, str) else ''} |")
    md += ["", f"![Utilization]({figs_rel}/utilization.png)", "", f"![Power]({figs_rel}/power.png)", "",
           f"![Savings]({figs_rel}/savings.png)", "", f"![Savings scatter]({figs_rel}/savings_scatter.png)", "",
           f"![Power breakdown]({figs_rel}/power_breakdown.png)", ""]
    md += ["### Application quality", "", "| Benchmark | Metric | Value | Threshold | Status |", "|:--|:--|--:|:--|:--|"]
    for r in results:
        a = r["app"]
        md.append(f"| {r['name']} | {a['name']} | {num(a['value'], 5)} {a['unit']} | {a['threshold']} | {({True: 'PASS', False: 'FAIL', None: 'n/a'})[a['pass']]} |")
    md += ["", "### Generalisation (held-out stream)", "", "| Benchmark | WCE tuning | WCE held-out | Held-out within limits |", "|:--|--:|--:|:--|"]
    for r in results:
        h = r.get("heldout")
        md.append(f"| {r['name']} | {r['errors']['wce']:.0f} | " + (f"{h['errors']['wce']:.0f} | {'yes' if h['within_limits'] else 'NO'} |" if h else "n/a | n/a |"))
    md += ["", f"![Held-out]({figs_rel}/heldout_vs_tuning.png)", "", f"![Candidates]({figs_rel}/candidates.png)", "",
           f"![Pareto]({figs_rel}/pareto.png)", ""]
    if extra_fig:
        md += [f"![Runtime]({figs_rel}/{extra_fig})", ""]
    return md


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--build-dir", default=None, help="Cluster build dir (contains dsp_on/, dsp_off/) for the runtime figures")
    args = ap.parse_args()
    run = Path(args.run_dir).resolve()
    build = Path(args.build_dir).resolve() if args.build_dir else None
    on, off = load(run / "dsp_on"), load(run / "dsp_off")
    md = ["# AxFPGA AxBench evaluation: final report", "",
          "Platform: Zybo Z7-10 (xc7z010clg400-1; larger designs on xc7z020). Synthesis: Yosys `synth_xilinx -family xc7`. "
          "Approximation: pyALS Candidate Tournament. LC = LUT + 2 x CARRY4. "
          "**WCE % = worst-case error / (2^bits - 1) x 100** on the relaxed output (bits = output width); "
          "every accepted design has its WCE proven by the formal check where the tier says PROVEN.", ""]
    n_ok = sum(1 for r in on if not r["fell_back"])
    if on:
        worst = max((r for r in on if r["errors"].get("wce_pct") is not None), key=lambda r: r["errors"]["wce_pct"], default=None)
        mean_lc = sum(r["savings_pct"]["logic_cells"] for r in on) / len(on)
        md += ["## Summary (DSP on)", "",
               f"- {len(on)} designs evaluated, {n_ok} approximated, {len(on) - n_ok} kept exact.",
               f"- Mean logic-cell saving {mean_lc:.1f}%; mean dynamic-power saving {sum(r['savings_pct']['dynamic_power_mw'] for r in on) / len(on):.1f}%.",
               *([f"- Largest WCE: {worst['name']} at {worst['errors']['wce_pct']:.4g} % of full scale."] if worst else []), ""]
    for mode, res, d in (("on", on, run / "dsp_on"), ("off", off, run / "dsp_off")):
        if not res:
            continue
        figs = d / "figures"
        subprocess.run([sys.executable, str(Path(__file__).with_name("generate_pyals_scienceplots.py")), "--out-dir", str(d)], check=True)
        subprocess.run([sys.executable, str(Path(__file__).with_name("generate_report_figures.py")), "--out-dir", str(d)], check=True)
        extra = runtime_fig(build / f"dsp_{mode}", figs, mode) if build else None
        rs = json.loads((d / "run_stats.json").read_text()) if (d / "run_stats.json").exists() else None
        md += section(mode, res, rs, f"dsp_{mode}/figures", extra)
    figs_final = run / "figures_final"
    figs_final.mkdir(exist_ok=True)
    fig = dsp_comparison_fig(on, off, figs_final) if on and off else None
    if fig:
        md += ["## DSP on vs off", "", f"![DSP comparison](figures_final/{fig})", ""]
        dc = run / "dsp_comparison.md"
        if dc.exists():
            md += [dc.read_text(), ""]
    (run / "FINAL_REPORT.md").write_text("\n".join(md) + "\n")
    print(f"Wrote {run / 'FINAL_REPORT.md'}")


if __name__ == "__main__":
    main()
