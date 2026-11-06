#!/usr/bin/env python3
"""Phase 5 report figures from docs/phases/phase-5 and reports/phase5 (PNG into docs/phases/phase-5/figures)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "phases" / "phase-5"
OUT = DOC / "figures"
BASE, A1, A2, A3 = "#8a8a86", "#2a78d6", "#eb6834", "#2e9e6a"
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
                     "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK,
                     "text.color": INK, "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold",
                     "axes.spines.top": False, "axes.spines.right": False})


def short(name: str) -> str:
    return name.replace("(behavioural *)", "bhv").replace("Layer", "").replace("Filter", "").strip()


def fig_savings():
    """Phase 4 vs Phase 5 LC saved, DSP off, per design."""
    p4 = {r["id"]: r for r in csv.DictReader(open(ROOT / "docs/phases/phase-4/dsp_comparison.csv"))}
    rows = []
    for r in json.load(open(ROOT / "reports/phase5/dsp_off/results.json")):
        rows.append((short(r["name"]), float(p4[r["id"]]["off_lc_saved_pct"]), r["savings_pct"]["logic_cells"],
                     r["certification"]["tier"]))
    rows = [x for x in rows if x[1] or x[2]]
    rows.sort(key=lambda x: -x[2])
    y = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(rows) + 1.2))
    ax.barh(y - 0.2, [x[1] for x in rows], 0.38, color=BASE, label="Phase 4")
    cols = [A1 if x[3] == "certified" else A2 for x in rows]
    ax.barh(y + 0.2, [x[2] for x in rows], 0.38, color=cols, label="Phase 5 (blue certified, orange uncertified)")
    ax.set_yticks(y, [x[0] for x in rows]); ax.invert_yaxis()
    ax.set_xlabel("Logic cells saved (%)"); ax.set_title("Logic cells saved per design, DSP inference off")
    ax.grid(axis="x", color=GRID, lw=0.6); ax.set_axisbelow(True); ax.legend(frameon=False, loc="lower right")
    fig.tight_layout(); fig.savefig(OUT / "savings_dsp_off.png", dpi=150); plt.close(fig)


def fig_savings_on():
    """Phase 4 vs Phase 5 LC saved, DSP on, per design."""
    p4 = {r["id"]: r for r in csv.DictReader(open(ROOT / "docs/phases/phase-4/dsp_comparison.csv"))}
    rows = []
    for r in json.load(open(ROOT / "reports/phase5/dsp_on/results.json")):
        rows.append((short(r["name"]), float(p4[r["id"]]["on_lc_saved_pct"]), r["savings_pct"]["logic_cells"],
                     r["certification"]["tier"]))
    rows = [x for x in rows if x[1] or x[2]]
    rows.sort(key=lambda x: -x[2])
    y = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(rows) + 1.2))
    ax.barh(y - 0.2, [x[1] for x in rows], 0.38, color=BASE, label="Phase 4")
    cols = [A1 if x[3] == "certified" else A2 for x in rows]
    ax.barh(y + 0.2, [x[2] for x in rows], 0.38, color=cols, label="Phase 5 (blue certified, orange uncertified)")
    ax.set_yticks(y, [x[0] for x in rows]); ax.invert_yaxis()
    ax.set_xlabel("Logic cells saved (%)"); ax.set_title("Logic cells saved per design, DSP inference on")
    ax.grid(axis="x", color=GRID, lw=0.6); ax.set_axisbelow(True); ax.legend(frameon=False, loc="lower right")
    fig.tight_layout(); fig.savefig(OUT / "savings_dsp_on.png", dpi=150); plt.close(fig)


def fig_ablation():
    """Grouped bars: LC saved per operator set."""
    abl = json.load(open(DOC / "ablation.json"))
    modes = [("off", "No operators", BASE), ("nocomp-nostack", "Truncation", A1), ("nocomp", "+ stacking", A3),
             ("nostack", "+ correction", A2), ("all", "+ both", "#7a4bb3")]
    rows = sorted(abl, key=lambda r: -max(r[m]["saved"] for m, *_ in modes if r.get(m) and r[m]))
    y = np.arange(len(rows)); h = 0.16
    fig, ax = plt.subplots(figsize=(8.5, 0.6 * len(rows) + 1.4))
    for i, (m, label, c) in enumerate(modes):
        vals = [(r[m]["saved"] if r.get(m) else 0) for r in rows]
        ax.barh(y + (i - 2) * h, vals, h, color=c, label=label)
    ax.set_yticks(y, [short(r["id"].replace("_", " ")) for r in rows]); ax.invert_yaxis()
    ax.set_xlabel("Logic cells saved (%)"); ax.set_title("Operator ablation (DSP off): what each ingredient adds")
    ax.grid(axis="x", color=GRID, lw=0.6); ax.set_axisbelow(True)
    ax.legend(frameon=False, ncol=5, fontsize=7, loc="lower center", bbox_to_anchor=(0.5, -0.22))
    fig.tight_layout(); fig.savefig(OUT / "ablation.png", dpi=150); plt.close(fig)


def fig_limiters():
    """Count of designs per limiting constraint, DSP off and on."""
    h = json.load(open(DOC / "headroom.json"))
    from collections import Counter
    labels = {"cone-bound": "Cone WCE bound", "wce": "Stream WCE", "mred": "MRED", "med": "MED", "app": "App metric",
              "held-out": "Held-out gate", "menu": "Candidate menu", "formal": "Formal proof",
              "search-schedule": "Search schedule"}
    keys = sorted({r["limiter"] for v in h.values() for r in v}, key=lambda k: -sum(
        1 for v in h.values() for r in v if r["limiter"] == k))
    off = Counter(r["limiter"] for r in h["dsp_off"]); on = Counter(r["limiter"] for r in h["dsp_on"])
    y = np.arange(len(keys)); w = 0.38
    fig, ax = plt.subplots(figsize=(7, 0.45 * len(keys) + 1.4))
    ax.barh(y - w / 2, [on[k] for k in keys], w, color=A1, label="DSP on")
    ax.barh(y + w / 2, [off[k] for k in keys], w, color=A2, label="DSP off")
    ax.set_yticks(y, [labels.get(k, k) for k in keys]); ax.invert_yaxis()
    ax.set_xlabel("Designs"); ax.set_title("What stops each design from saving more")
    ax.grid(axis="x", color=GRID, lw=0.6); ax.set_axisbelow(True); ax.legend(frameon=False)
    fig.tight_layout(); fig.savefig(OUT / "limiters.png", dpi=150); plt.close(fig)


def fig_rg():
    """Random genomes: control and WCE proof outcomes per design."""
    d = json.load(open(ROOT / "reports/phase5/random_genomes/random_genomes.json"))
    from collections import Counter
    rows = []
    ctrl_by = {}
    for s in sorted(d, key=lambda s: s["id"]):
        c = Counter(r.get("control", "-") for r in s.get("rows", []))
        ctrl_by[s["id"]] = dict(c)
        w = Counter(r.get("wce", "-") for r in s.get("rows", []))
        rows.append((s["id"], s["processed"], c.get("PROVEN", 0), c.get("FALSIFIED", 0), w.get("PROVEN", 0),
                     w.get("FALSIFIED", 0), w.get("TIMEOUT", 0)))
    names = [r[0] for r in rows]; y = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8, 0.36 * len(rows) + 1.4))
    left = np.zeros(len(rows))
    for idx, (label, color) in enumerate([("WCE PROVEN", A3), ("WCE FALSIFIED (expected: over limit)", "#bdbdb8"),
                                          ("WCE TIMEOUT", A2)]):
        vals = [r[4 + idx] for r in rows] if idx < 2 else [r[6] for r in rows]
        if idx == 1:
            vals = [r[5] for r in rows]
        ax.barh(y, vals, left=left, color=color, label=label); left += np.array(vals)
    for i, r in enumerate(rows):
        ctrl = ", ".join(f"{k} {v}" for k, v in sorted(ctrl_by[r[0]].items()))
        ax.text(left[i] + 2, i, f"control {ctrl}", va="center", fontsize=7, color=INK2)
    ax.set_yticks(y, names); ax.invert_yaxis(); ax.set_xlim(0, max(left) * 1.35)
    ax.set_xlabel("Random genomes"); ax.set_title("Random-genome check: control proofs and WCE proofs")
    ax.grid(axis="x", color=GRID, lw=0.6); ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3)
    fig.tight_layout(); fig.savefig(OUT / "random_genomes.png", dpi=150); plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    fig_savings(); fig_savings_on(); fig_ablation(); fig_limiters(); fig_rg()
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))
