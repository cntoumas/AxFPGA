#!/usr/bin/env python3
"""Supervisor report from a suite run: tables (Markdown), figures (PNG) and a flat JSON for the dashboard."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

INK, INK2, GRID = "#1f2933", "#5f6c7b", "#e3e7ec"
EXACT, APPROX, GOOD, WARN, BAD = "#9aa5b1", "#1f6feb", "#2a9d6f", "#d9962b", "#c8453b"
TIER_COLOR = {"certified": GOOD, "bounded-certified": WARN, "uncertified": "#9aa5b1", "exact": "#cbd2d9"}

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.dpi": 150})


def f(v: Optional[float], nd: int = 1, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v:,.{nd}f}{suffix}"


def short(name: str) -> str:
    return (name.replace("Multiplier", "Mult").replace("_", " ").replace("bhv", "(*)")
            .replace("kinematics", "kin.").replace("neural network", "NN").replace("kmeans clustering", "k-means"))


def flatten(r: Dict[str, Any]) -> Dict[str, Any]:
    ex, ap, sv, er, ce = r["exact"], r["approx"], r["savings_pct"], r["errors"], r["certification"]
    lim = r["limits"]
    held = r.get("heldout") or {}
    tier = "exact" if r["fell_back"] else ce["tier"]
    tr = r.get("through_registers") or {}
    return {
        "id": r["id"], "name": r["name"], "domain": r["domain"], "device": r["device"],
        "method": "exact design kept" if r["fell_back"] else r["method"].replace(" [raw]", "").replace(" [remapped]", ""),
        "fell_back": r["fell_back"], "tier": tier,
        "lc_exact": ex["logic_cells"], "lc_approx": ap["logic_cells"], "lc_saved": sv["logic_cells"],
        "lut_exact": ex["luts"], "lut_approx": ap["luts"], "ff_exact": ex["ffs"], "ff_approx": ap["ffs"],
        "dsp_exact": ex["dsps"], "dsp_approx": ap["dsps"], "bram_exact": ex["brams"], "bram_approx": ap["brams"],
        "carry_exact": ex["carry4"], "carry_approx": ap["carry4"],
        "p_exact": ex["dynamic_power_mw"], "p_approx": ap["dynamic_power_mw"], "p_saved": sv["dynamic_power_mw"],
        "lut_saved": sv["luts"], "ff_saved": sv["ffs"], "dsp_saved": sv["dsps"],
        "wce": er.get("wce"), "med": er.get("med"), "mred": er.get("mred_pct"), "er": er.get("er"),
        "wce_pct": er.get("wce_pct"), "out_bits": er.get("out_bits"),
        "lim_wce": lim.get("wce"), "lim_med": lim.get("med"), "lim_mred": lim.get("mred_pct"), "lim_app": lim.get("app"),
        "app_name": r["app"]["name"], "app_value": r["app"]["value"], "app_unit": r["app"]["unit"],
        "app_pass": r["app"]["pass"], "app_thr": r["app"]["threshold"],
        "held_ok": held.get("within_limits") if held else None, "held_n": held.get("samples") if held else None,
        "ctrl": r["formal"]["control_equivalence"].split(" ")[0], "wce_proof": r["formal"]["wce_bound"].split(" ")[0],
        "cert_reason": ce.get("reason", ""),
        "registers_sites": (tr.get("sites_behind_registers") if isinstance(tr, dict) else None),
        "quality": r.get("quality") or [],
    }


def limit_use(row: Dict[str, Any]) -> Optional[float]:
    """Largest fraction of a declared error limit used by the accepted design (tuning stream), in %."""
    best = None
    for q in row["quality"]:
        if q["metric"] in ("wce", "med", "mred") and q["threshold"] and q["value"] is not None:
            u = 100.0 * q["value"] / q["threshold"]
            best = u if best is None else max(best, u)
    return best


def barh_savings(rows: List[Dict[str, Any]], key: str, title: str, fname: str, out: Path, color: str = APPROX) -> None:
    rows = sorted(rows, key=lambda r: r[key])
    fig, ax = plt.subplots(figsize=(7.2, 0.30 * len(rows) + 1.2))
    y = np.arange(len(rows))
    vals = [r[key] for r in rows]
    ax.barh(y, vals, color=[color if v > 0 else GRID for v in vals], height=0.68)
    for i, v in enumerate(vals):
        ax.text(max(v, 0) + 0.6, i, f"{v:.1f}%", va="center", fontsize=8, color=INK2)
    ax.set_yticks(y, [short(r["id"]) for r in rows])
    ax.set_xlabel("saved vs exact design (%)")
    ax.set_title(title, loc="left", fontsize=10.5, fontweight="bold")
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.set_xlim(0, max(vals + [5]) * 1.12)
    fig.tight_layout()
    fig.savefig(out / fname)
    plt.close(fig)


def fig_lc(rows: List[Dict[str, Any]], out: Path) -> None:
    rows = sorted(rows, key=lambda r: -r["lc_exact"])
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    x = np.arange(len(rows))
    w = 0.4
    ax.bar(x - w / 2, [r["lc_exact"] for r in rows], w, color=EXACT, label="exact")
    ax.bar(x + w / 2, [r["lc_approx"] for r in rows], w, color=APPROX, label="approximate")
    ax.set_yscale("log")
    ax.set_xticks(x, [short(r["id"]) for r in rows], rotation=60, ha="right", fontsize=7.5)
    ax.set_ylabel("logic cells (LUT + 2 x CARRY4), log scale")
    ax.set_title("Logic cells: exact vs approximate", loc="left", fontsize=10.5, fontweight="bold")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(out / "logic_cells.png")
    plt.close(fig)


def fig_power(rows: List[Dict[str, Any]], out: Path) -> None:
    rows = sorted(rows, key=lambda r: -r["p_exact"])
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    x = np.arange(len(rows))
    w = 0.4
    ax.bar(x - w / 2, [r["p_exact"] for r in rows], w, color=EXACT, label="exact")
    ax.bar(x + w / 2, [r["p_approx"] for r in rows], w, color=APPROX, label="approximate")
    ax.set_yscale("log")
    ax.set_xticks(x, [short(r["id"]) for r in rows], rotation=60, ha="right", fontsize=7.5)
    ax.set_ylabel("dynamic power (mW, indicative), log scale")
    ax.set_title("Dynamic power: exact vs approximate (Yosys activity model, 256 cycles)", loc="left", fontsize=10.5,
                 fontweight="bold")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(out / "power.png")
    plt.close(fig)


def fig_error_use(rows: List[Dict[str, Any]], out: Path) -> None:
    rows = [r for r in rows if not r["fell_back"] and limit_use(r) is not None]
    rows = sorted(rows, key=lambda r: limit_use(r))
    fig, ax = plt.subplots(figsize=(7.2, 0.30 * len(rows) + 1.2))
    y = np.arange(len(rows))
    vals = [limit_use(r) for r in rows]
    ax.barh(y, vals, color=[BAD if v > 100 else APPROX for v in vals], height=0.68)
    ax.axvline(100, color=INK, lw=1)
    ax.text(100, len(rows) - 0.3, " declared limit", fontsize=8, color=INK2, va="bottom")
    ax.set_yticks(y, [short(r["id"]) for r in rows])
    ax.set_xlabel("worst of WCE / MED / MRED as % of its declared limit (tuning stream)")
    ax.set_title("How much of the error budget each design uses", loc="left", fontsize=10.5, fontweight="bold")
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.set_xlim(0, 118)
    fig.tight_layout()
    fig.savefig(out / "error_budget.png")
    plt.close(fig)


def fig_cert(rows: List[Dict[str, Any]], out: Path) -> None:
    order = ["certified", "bounded-certified", "uncertified", "exact"]
    labels = {"certified": "certified (proofs PROVEN)", "bounded-certified": "bounded-certified",
              "uncertified": "uncertified (proof timed out / not applicable)", "exact": "exact design kept"}
    counts = [sum(1 for r in rows if r["tier"] == t) for t in order]
    fig, ax = plt.subplots(figsize=(7.2, 2.3))
    left = 0
    for t, c in zip(order, counts):
        if c:
            ax.barh([0], [c], left=left, color=TIER_COLOR[t], height=0.55)
            ax.text(left + c / 2, 0, str(c), ha="center", va="center", color="white", fontweight="bold")
            left += c
    ax.set_yticks([])
    ax.set_xlim(0, len(rows))
    ax.set_xticks(range(0, len(rows) + 1, 3))
    ax.set_xlabel("designs")
    ax.set_title("Formal certification tier", loc="left", fontsize=10.5, fontweight="bold")
    handles = [plt.Rectangle((0, 0), 1, 1, color=TIER_COLOR[t]) for t, c in zip(order, counts) if c]
    ax.legend(handles, [labels[t] for t, c in zip(order, counts) if c], frameon=False, ncol=2, fontsize=7.5,
              loc="upper center", bbox_to_anchor=(0.5, -0.75))
    fig.tight_layout()
    fig.savefig(out / "certification.png")
    plt.close(fig)


def fig_util(rows: List[Dict[str, Any]], out: Path) -> None:
    """Heatmap: percent change of each resource per design (positive = saved)."""
    from matplotlib.colors import LinearSegmentedColormap
    cols = [("lc_exact", "lc_approx", "Logic cells"), ("lut_exact", "lut_approx", "LUT"), ("ff_exact", "ff_approx", "FF"),
            ("dsp_exact", "dsp_approx", "DSP48"), ("carry_exact", "carry_approx", "CARRY4"),
            ("p_exact", "p_approx", "Power")]
    rows = sorted(rows, key=lambda r: -r["lc_saved"])
    m = np.full((len(rows), len(cols)), np.nan)
    for i, r in enumerate(rows):
        for j, (e, a, _) in enumerate(cols):
            if r[e] > 0:
                m[i, j] = 100.0 * (r[e] - r[a]) / r[e]
    cmap = LinearSegmentedColormap.from_list("sv", [BAD, "#f4f6f8", APPROX])
    fig, ax = plt.subplots(figsize=(7.2, 0.30 * len(rows) + 1.5))
    ax.imshow(np.nan_to_num(m, nan=0.0), cmap=cmap, vmin=-60, vmax=60, aspect="auto")
    for i in range(len(rows)):
        for j in range(len(cols)):
            v = m[i, j]
            ax.text(j, i, "-" if np.isnan(v) else f"{v:.0f}", ha="center", va="center", fontsize=7.5,
                    color=INK if np.isnan(v) or abs(v) < 40 else "white")
    ax.set_xticks(range(len(cols)), [c[2] for c in cols])
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(rows)), [short(r["id"]) for r in rows])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0)
    ax.set_title("Resources saved per design (%)", loc="left", fontsize=10.5, fontweight="bold", pad=24)
    ax.set_xlabel("blue = fewer than exact, red = more than exact, - = resource not used", fontsize=8)
    fig.tight_layout()
    fig.savefig(out / "utilization.png")
    plt.close(fig)


def md_table(header: List[str], body: List[List[str]], left: int = 1) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(":--" if i < left else "--:" for i in range(len(header))) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def build(run: Path, out: Path) -> None:
    results = json.loads((run / "results.json").read_text())
    rows = [flatten(r) for r in results]
    rows.sort(key=lambda r: (-r["lc_saved"], r["id"]))
    figs = out / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    approx = [r for r in rows if not r["fell_back"]]
    saved = [r["lc_saved"] for r in approx]
    n_cert = sum(1 for r in rows if r["tier"] == "certified")
    held_ok = sum(1 for r in approx if r["held_ok"])
    app_ok = sum(1 for r in approx if r["app_pass"])
    summary = {
        "designs": len(rows), "approximated": len(approx), "kept_exact": len(rows) - len(approx),
        "mean_lc_saved_all": statistics.mean(r["lc_saved"] for r in rows),
        "mean_lc_saved_approx": statistics.mean(saved) if saved else 0.0,
        "median_lc_saved_approx": statistics.median(saved) if saved else 0.0,
        "max_lc_saved": max(saved) if saved else 0.0, "max_lc_design": max(approx, key=lambda r: r["lc_saved"])["id"] if approx else "",
        "mean_power_saved_approx": statistics.mean(r["p_saved"] for r in approx) if approx else 0.0,
        "certified": n_cert, "bounded": sum(1 for r in rows if r["tier"] == "bounded-certified"),
        "uncertified": sum(1 for r in rows if r["tier"] == "uncertified"),
        "heldout_ok": held_ok, "app_ok": app_ok,
        "lc_exact_total": sum(r["lc_exact"] for r in rows), "lc_approx_total": sum(r["lc_approx"] for r in rows),
    }
    (out / "report_data.json").write_text(json.dumps({"summary": summary, "rows": [{k: v for k, v in r.items() if k != "quality"} | {"limit_use": limit_use(r)} for r in rows]}, indent=1))

    barh_savings(approx, "lc_saved", "Logic cells saved per design", "savings_lc.png", figs)
    barh_savings(approx, "p_saved", "Dynamic power saved per design (indicative)", "savings_power.png", figs, color="#8250df")
    fig_lc(rows, figs)
    fig_power(rows, figs)
    fig_error_use(rows, figs)
    fig_cert(rows, figs)
    fig_util(approx, figs)

    md: List[str] = []
    md += ["# AxFPGA: results of the full suite", "",
           f"Target: Zybo Z7-10 (xc7z010clg400-1; the 89-multiplier designs on xc7z020). Synthesis: Yosys `synth_xilinx -family xc7` with DSP48 inference on. "
           f"Cost unit: logic cells (LC = LUT + 2 x CARRY4). Power: activity-based Yosys estimate over 256 cycles, indicative only (Vivado runs decide). "
           f"Approximation: pyALS candidates, operand/output truncation with correction, stacking, and relaxation behind internal registers.", "",
           "## Headline", "",
           md_table(["Measure", "Value"], [
               ["Designs evaluated", str(summary["designs"])],
               ["Approximated / kept exact", f"{summary['approximated']} / {summary['kept_exact']}"],
               ["Mean logic cells saved (approximated designs)", f"{summary['mean_lc_saved_approx']:.1f} % (median {summary['median_lc_saved_approx']:.1f} %)"],
               ["Best design", f"{summary['max_lc_design']} at {summary['max_lc_saved']:.1f} %"],
               ["Mean dynamic power saved (approximated designs)", f"{summary['mean_power_saved_approx']:.1f} %"],
               ["Application metric met", f"{summary['app_ok']} of {summary['approximated']}"],
               ["Held-out stream within limits", f"{summary['heldout_ok']} of {summary['approximated']}"],
               ["Formally certified (control + WCE PROVEN)", f"{summary['certified']} of {summary['designs']}"],
           ]), "",
           "![Logic cells saved](figures/savings_lc.png)", "",
           "## 1. Savings and resources", ""]
    body = []
    for r in rows:
        body.append([f"**{r['id']}**", short(r["domain"]) if False else r["domain"], f"{r['lc_exact']} -> {r['lc_approx']}", f"**{r['lc_saved']:.1f}**",
                     f"{r['lut_exact']} -> {r['lut_approx']}", f"{r['ff_exact']} -> {r['ff_approx']}",
                     f"{r['dsp_exact']} -> {r['dsp_approx']}", f"{r['carry_exact']} -> {r['carry_approx']}"])
    md += [md_table(["Design", "Domain", "Logic cells", "Saved %", "LUT", "FF", "DSP48", "CARRY4"], body, left=2), "",
           "![Logic cells](figures/logic_cells.png)", "", "![Resources saved](figures/utilization.png)", "",
           "## 2. Power (indicative)", ""]
    body = [[f"**{r['id']}**", f"{r['p_exact']:.2f}", f"{r['p_approx']:.2f}", f"{r['p_saved']:.1f}"] for r in rows]
    md += [md_table(["Design", "Exact mW", "Approx mW", "Saved %"], body), "",
           "![Power](figures/power.png)", "", "![Power saved](figures/savings_power.png)", "",
           "## 3. Error metrics", "",
           "Error is measured on the application stream, in the relaxed output's own number format (LSB units). WCE % is the worst-case error as a percentage of the output's full-scale range. "
           "Limits are the declared per-design bounds (`axc_bound`); an approximation is accepted only when every limit holds on the tuning stream and on a held-out stream.", ""]
    body = []
    for r in approx:
        body.append([f"**{r['id']}**", r["method"], f(r["wce"], 0), f(r["lim_wce"], 0), f(r["med"], 2), f(r["lim_med"], 1),
                     f(r["mred"], 2), f(r["lim_mred"], 1), f(r["wce_pct"], 3), f(r["er"] * 100 if r["er"] is not None else None, 1)])
    md += [md_table(["Design", "Method", "WCE", "WCE limit", "MED", "MED limit", "MRED %", "MRED limit %", "WCE % of range", "Error rate %"], body, left=2), "",
           "![Error budget](figures/error_budget.png)", "",
           "## 4. Application metrics", ""]
    body = []
    for r in approx:
        v = r["app_value"]
        vs = "exact match" if v is None else f"{v:.3f}"
        body.append([f"**{r['id']}**", r["app_name"], vs + (f" {r['app_unit']}" if v is not None else ""), r["app_thr"] or "none",
                     "pass" if r["app_pass"] else ("n/a" if r["app_pass"] is None else "FAIL"),
                     "pass" if r["held_ok"] else ("n/a" if r["held_ok"] is None else "FAIL"), str(r["held_n"] or "")])
    md += [md_table(["Design", "Metric", "Value", "Required", "Tuning stream", "Held-out", "Held-out samples"], body, left=2), "",
           "## 5. Formal certification", "",
           "Control equivalence: the precise outputs are proven identical to the exact design (Yosys SAT). WCE bound: the cone's worst-case error is proven below its bound. "
           "Designs where the SAT proof times out, or no combinational proof applies (arithmetic behind registers, sequential accumulation), are reported as uncertified; sequential proofs are not implemented yet.", ""]
    body = [[f"**{r['id']}**", r["ctrl"], r["wce_proof"], r["tier"], (r["cert_reason"] or "")[:90]] for r in rows]
    md += [md_table(["Design", "Control proof", "WCE proof", "Tier", "Why not certified"], body, left=5), "",
           "![Certification](figures/certification.png)", "",
           "## 6. Chosen approximation per design", ""]
    body = [[f"**{r['id']}**", r["method"]] for r in rows]
    md += [md_table(["Design", "Method"], body, left=2), ""]
    (out / "REPORT.md").write_text("\n".join(md) + "\n")
    print(f"Wrote {out / 'REPORT.md'} ({len(rows)} designs), {len(list(figs.glob('*.png')))} figures")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build(Path(a.run), Path(a.out))
