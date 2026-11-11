#!/usr/bin/env python3
"""Additional figures for the AxBench Zybo Z7-10 evaluation (supervisor report)."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BASE, APPROX, OK, BAD = "#2a78d6", "#eb6834", "#2a9d5c", "#c8372d"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK,
    "text.color": INK, "font.size": 8, "axes.titlesize": 9, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False,
})


def _ratio(value, limit):
    if value is None or limit in (None, 0):
        return None
    return float(value) / float(limit)


def error_vs_bound(data, figs):
    """Measured error as a fraction of the user bound (1.0 = bound reached)."""
    names = [d["name"] for d in data]
    series = [("WCE", "wce", BASE), ("MED", "med", APPROX), ("MRED", "mred_pct", OK)]
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(names) + 1.6), dpi=200)
    y = np.arange(len(names))
    h = 0.26
    for k, (label, key, color) in enumerate(series):
        vals = []
        for d in data:
            r = _ratio(d["errors"].get(key), d["limits"].get(key))
            vals.append(max(r, 1e-6) if r is not None else np.nan)
        ax.barh(y + (k - 1) * h, vals, h, color=color, label=label, edgecolor=SURFACE, linewidth=0.8)
    ax.axvline(1.0, color=BAD, linewidth=1, linestyle="--")
    ax.text(1.0, -0.9, "bound", color=BAD, fontsize=7, ha="center")
    ax.set_xscale("log")
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_xlabel("measured error / allowed bound (log scale; below 1 = inside the bound)")
    ax.set_title("Error budget used per benchmark", loc="left")
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.04), ncol=3, frameon=False)
    fig.tight_layout()
    fig.savefig(figs / "error_vs_bound.png")
    plt.close(fig)


def pareto(data, figs):
    """Per benchmark: logic cells vs worst-case error of every candidate the tournament tried."""
    items = [d for d in data if d.get("attempts")]
    cols = 4
    rows = math.ceil(len(items) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(3.2 * cols, 2.6 * rows), dpi=200, squeeze=False)
    for ax in axes.ravel():
        ax.set_visible(False)
    for ax, d in zip(axes.ravel(), items):
        ax.set_visible(True)
        pts = [(a["lcs"], a["wce"], a["outcome"]) for a in d["attempts"] if a.get("lcs") is not None and a.get("wce") is not None]
        base_lc = d["exact"]["logic_cells"]
        for lcs, wce, outcome in pts:
            ax.scatter(lcs, max(wce, 0.5), s=26 if outcome != "accepted" else 70,
                       color=OK if outcome == "accepted" else (BASE if outcome == "passed checks" else BAD),
                       marker="*" if outcome == "accepted" else "o", edgecolor=SURFACE, linewidth=0.5, zorder=3)
        ax.axvline(base_lc, color=INK2, linestyle=":", linewidth=1)
        if d["limits"].get("wce"):
            ax.axhline(d["limits"]["wce"], color=BAD, linestyle="--", linewidth=0.8)
        ax.set_yscale("log")
        ax.set_title(d["name"], loc="left", fontsize=8)
        ax.set_xlabel("logic cells", fontsize=7)
        ax.set_ylabel("WCE (log)", fontsize=7)
        ax.grid(color=GRID, linewidth=0.5)
        ax.set_axisbelow(True)
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker="*", color=OK, linestyle="", markersize=9, label="accepted champion"),
               Line2D([], [], marker="o", color=BASE, linestyle="", label="passed checks"),
               Line2D([], [], marker="o", color=BAD, linestyle="", label="rejected"),
               Line2D([], [], color=INK2, linestyle=":", label="exact design LCs"),
               Line2D([], [], color=BAD, linestyle="--", label="WCE bound")]
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False)
    fig.suptitle("Tournament candidates: area vs worst-case error", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    fig.savefig(figs / "pareto.png")
    plt.close(fig)


def power_breakdown(data, figs):
    """Dynamic power per primitive class, exact (left bar) vs approximate (right bar)."""
    classes = sorted({c for d in data for part in ("exact", "approx") for c in d[part].get("power_breakdown", {})})
    palette = ["#2a78d6", "#eb6834", "#2a9d5c", "#8a5cc2", "#d9a400", "#1fa7b5", "#a0a0a0", "#c8372d"]
    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=200)
    x = np.arange(len(data))
    w = 0.38
    for part, off, hatch in (("exact", -w / 2, ""), ("approx", w / 2, "//")):
        bottom = np.zeros(len(data))
        for i, c in enumerate(classes):
            vals = np.array([d[part].get("power_breakdown", {}).get(c, {}).get("power_mw", 0.0) for d in data])
            ax.bar(x + off, vals, w, bottom=bottom, color=palette[i % len(palette)], hatch=hatch,
                   edgecolor=SURFACE, linewidth=0.5, label=c if part == "exact" else None)
            bottom += vals
    ax.set_yscale("symlog", linthresh=1)
    ax.set_xticks(x)
    ax.set_xticklabels([d["name"] for d in data], rotation=30, ha="right")
    ax.set_ylabel("mW (symlog)")
    ax.set_title("Dynamic power by primitive class (solid = exact, hatched = approximate)", loc="left")
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(ncol=len(classes), frameon=False, loc="lower left", bbox_to_anchor=(0, 1.02), fontsize=7)
    fig.tight_layout()
    fig.savefig(figs / "power_breakdown.png")
    plt.close(fig)


def savings_scatter(data, figs):
    """Logic-cell savings vs power savings, one point per benchmark."""
    fig, ax = plt.subplots(figsize=(6.4, 5), dpi=200)
    for d in data:
        lc, pw = d["savings_pct"]["logic_cells"], d["savings_pct"]["dynamic_power_mw"]
        ax.scatter(lc, pw, s=60, color=APPROX if not d.get("fell_back") else INK2, edgecolor=SURFACE, zorder=3)
        ax.annotate(d["name"], (lc, pw), textcoords="offset points", xytext=(5, 4), fontsize=7, color=INK2)
    lim = max(10, max(max(d["savings_pct"]["logic_cells"], d["savings_pct"]["dynamic_power_mw"]) for d in data) * 1.1)
    ax.plot([0, lim], [0, lim], color=GRID, linestyle="--", linewidth=1, zorder=1)
    ax.set_xlim(-2, lim)
    ax.set_ylim(-2, lim)
    ax.set_xlabel("Logic-cell savings (%)")
    ax.set_ylabel("Dynamic-power savings (%)")
    ax.set_title("Area savings vs power savings (grey = exact design kept)", loc="left")
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(figs / "savings_scatter.png")
    plt.close(fig)


def candidates(data, figs):
    """Candidates tried per benchmark, split by outcome."""
    names = [d["name"] for d in data]
    kinds = [("accepted", OK), ("passed checks", BASE), ("rejected", BAD)]
    fig, ax = plt.subplots(figsize=(7, 0.42 * len(names) + 1.4), dpi=200)
    y = np.arange(len(names))
    left = np.zeros(len(names))
    for kind, color in kinds:
        v = np.array([sum(1 for a in d.get("attempts", []) if a["outcome"] == kind) for d in data], dtype=float)
        ax.barh(y, v, left=left, color=color, label=kind, edgecolor=SURFACE, linewidth=0.8)
        left += v
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlabel("candidates evaluated")
    ax.set_title("Candidate tournament outcomes", loc="left")
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.04), ncol=3, frameon=False)
    fig.tight_layout()
    fig.savefig(figs / "candidates.png")
    plt.close(fig)


def wce_percent(data, figs):
    """Worst-case error of the accepted design in percent of the output full-scale range."""
    rows = [(d["name"], d["errors"].get("wce_pct"), d.get("fell_back")) for d in data if d["errors"].get("wce_pct") is not None]
    if not rows:
        return
    names = [r[0] for r in rows]
    vals = np.array([r[1] for r in rows], dtype=float)
    fig, ax = plt.subplots(figsize=(7.5, 0.42 * len(names) + 1.5), dpi=200)
    y = np.arange(len(names))
    floor = 1e-9
    ax.barh(y, np.maximum(vals, floor), 0.62, color=[INK2 if r[2] else APPROX for r in rows], edgecolor=SURFACE)
    for yi, v in zip(y, vals):
        ax.text(max(v, floor) * 1.3, yi, f"{v:.4g} %", va="center", fontsize=7, color=INK)
    ax.set_xscale("log")
    ax.set_xlim(floor, max(vals.max(), 1e-3) * 400)
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_xlabel("WCE, % of output full-scale range (log; grey = exact design kept, WCE 0)")
    ax.set_title("Worst-case error of the approximate output (percent)", loc="left")
    fig.tight_layout()
    fig.savefig(figs / "wce_percent.png")
    plt.close(fig)


def wce_vs_savings(data, figs):
    """Trade-off: WCE % (x, log) against logic-cell savings (y)."""
    pts = [d for d in data if d["errors"].get("wce_pct") is not None and not d.get("fell_back")]
    if not pts:
        return
    fig, ax = plt.subplots(figsize=(6.6, 4.8), dpi=200)
    for d in pts:
        x, yv = max(d["errors"]["wce_pct"], 1e-7), d["savings_pct"]["logic_cells"]
        ax.scatter(x, yv, s=60, color=APPROX, edgecolor=SURFACE, zorder=3)
        ax.annotate(d["name"], (x, yv), textcoords="offset points", xytext=(5, 4), fontsize=7, color=INK2)
    ax.set_xscale("log")
    ax.set_xlabel("WCE, % of output full-scale range (log)")
    ax.set_ylabel("Logic-cell savings (%)")
    ax.set_title("Accuracy vs area trade-off (approximated designs)", loc="left")
    ax.grid(color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(figs / "wce_vs_savings.png")
    plt.close(fig)


def heldout_vs_tuning(data, figs):
    """WCE on the tuning stimulus vs the independent held-out stream."""
    rows = [(d["name"], d["errors"]["wce"], d["heldout"]["errors"]["wce"]) for d in data if d.get("heldout")]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(rows) + 1.6), dpi=200)
    y = np.arange(len(rows))
    h = 0.36
    ax.barh(y - h / 2, [max(r[1], 0.5) for r in rows], h, color=BASE, label="tuning stimulus", edgecolor=SURFACE)
    ax.barh(y + h / 2, [max(r[2], 0.5) for r in rows], h, color=APPROX, label="held-out stream", edgecolor=SURFACE)
    ax.set_xscale("log")
    ax.set_yticks(y)
    ax.set_yticklabels([r[0] for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("WCE (LSB units, log; zero plotted at 0.5)")
    ax.set_title("Generalisation: tuning vs held-out worst-case error", loc="left")
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.04), ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(figs / "heldout_vs_tuning.png")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", "-o", required=True)
    args = ap.parse_args()
    out = Path(args.out_dir).resolve()
    figs = out / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    data = json.loads((out / "results.json").read_text())
    for fn in (error_vs_bound, pareto, power_breakdown, savings_scatter, candidates, wce_percent, wce_vs_savings, heldout_vs_tuning):
        fn(data, figs)
    print(f"Extra figures written to {figs}")


if __name__ == "__main__":
    main()
