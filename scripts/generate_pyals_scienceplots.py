#!/usr/bin/env python3
"""Figures for the AxBench Zybo Z7-10 evaluation.

Reads <out-dir>/results.json (written by run_pyals_yosys_suite.py) and writes
<out-dir>/figures/{utilization,power,savings}.png. Nothing is written outside out-dir.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BASE, APPROX = "#2a78d6", "#eb6834"          # categorical slots 1, 2 (validated pair)
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK,
    "text.color": INK, "font.size": 8, "axes.titlesize": 9, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False,
})


def _grouped_barh(ax, names, base, approx, title, log=False, unit=""):
    y = np.arange(len(names))
    h = 0.38
    ax.barh(y - h / 2 - 0.01, base, h, color=BASE, label="Baseline", edgecolor=SURFACE, linewidth=1)
    ax.barh(y + h / 2 + 0.01, approx, h, color=APPROX, label="Approximate", edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_title(title, loc="left")
    top = max(max(base), max(approx))
    if top == 0:
        ax.set_xlim(0, 1)
        ax.set_xticks([0, 1])
        ax.text(0.5, 0.5, "none used", transform=ax.transAxes, ha="center", va="center", color=INK2)
    elif log:
        ax.set_xscale("symlog", linthresh=1)
    else:
        from matplotlib.ticker import MaxNLocator
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    if unit:
        ax.set_xlabel(unit)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "reports"))
    args = ap.parse_args()
    out = Path(args.out_dir).resolve()
    figs = out / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    data = json.loads((out / "results.json").read_text())
    names = [d["name"] for d in data]

    # 1. Utilization small multiples
    panels = [("luts", "LUTs", True), ("ffs", "Flip-flops", True), ("dsps", "DSP48E1", False),
              ("brams", "Block RAM", False), ("logic_cells", "Logic cells (LUT + 2xCARRY4)", True)]
    fig, axes = plt.subplots(1, len(panels), figsize=(15, 0.42 * len(names) + 1.6), sharey=True, dpi=200)
    for ax, (k, title, log) in zip(axes, panels):
        _grouped_barh(ax, names, [d["exact"][k] for d in data], [d["approx"][k] for d in data], title, log=log)
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.06), ncol=2, frameon=False)
    fig.suptitle("Resource utilization on Zybo Z7-10 (xc7z010), Yosys synth_xilinx", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(figs / "utilization.png")
    plt.close(fig)

    # 2. Dynamic power
    fig, ax = plt.subplots(figsize=(7, 0.42 * len(names) + 1.4), dpi=200)
    f_clk = data[0]["power_model"]["f_clk_mhz"] if data else 125
    _grouped_barh(ax, names, [d["exact"]["dynamic_power_mw"] for d in data], [d["approx"]["dynamic_power_mw"] for d in data],
                  f"Estimated dynamic power @ {f_clk:.0f} MHz (activity-based)", log=True, unit="mW (symlog)")
    ax.legend(loc="lower right", frameon=False)
    fig.tight_layout()
    fig.savefig(figs / "power.png")
    plt.close(fig)

    # 3. Savings with application-metric outcome
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(names) + 1.4), dpi=200)
    y = np.arange(len(names))
    h = 0.38
    lc = [d["savings_pct"]["logic_cells"] for d in data]
    pw = [d["savings_pct"]["dynamic_power_mw"] for d in data]
    ax.barh(y - h / 2 - 0.01, lc, h, color=BASE, label="Logic-cell savings", edgecolor=SURFACE, linewidth=1)
    ax.barh(y + h / 2 + 0.01, pw, h, color=APPROX, label="Dynamic-power savings", edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.axvline(0, color=INK2, linewidth=0.8)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_xlabel("Savings vs baseline (%)")
    ax.set_title("Savings and application-metric outcome", loc="left")
    xmax = max([1.0] + lc + pw)
    for i, d in enumerate(data):
        a = d["app"]
        v = a["value"]
        vs = "inf" if isinstance(v, float) and math.isinf(v) else (f"{v:.3g}" if isinstance(v, (int, float)) and v is not None else str(v))
        tag = {True: "PASS", False: "FAIL", None: ""}[a["pass"]]
        label = "exact design kept (no candidate met every threshold)" if d.get("fell_back") else f"{a['name']}: {vs} {a['unit']} {tag}".strip()
        ax.text(xmax * 1.04, i, label, va="center", fontsize=7, color=INK2)
    ax.set_xlim(right=xmax * 2.3)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.14), ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(figs / "savings.png")
    plt.close(fig)
    print(f"Figures written to {figs}")


if __name__ == "__main__":
    main()
