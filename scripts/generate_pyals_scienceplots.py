#!/usr/bin/env python3
"""Generate publication-quality SciencePlots figures for Yosys + pyALS AxBench results."""

import json
import shutil
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

# Apply SciencePlots / clean publication style
try:
    import scienceplots
    plt.style.use(["science", "nature", "no-latex"])
except Exception:
    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")

REPORTS_DIR = Path("/home/christos-ntoumas/AxFPGA/reports")
FIGURES_DIR = REPORTS_DIR / "figures"
ARTIFACT_DIR = Path("/home/christos-ntoumas/.gemini/antigravity-cli/brain/cec24320-89c5-4ed7-82c0-2ec5456f3a4c")

FIGURES_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

JSON_FILE = REPORTS_DIR / "pyals_yosys_benchmark_results.json"
if not JSON_FILE.exists():
    print(f"Error: {JSON_FILE} not found!")
    exit(1)

with open(JSON_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

bench_names = [d["name"] for d in data]
lut_savings = [d["savings"]["lut_saved_pct"] for d in data]
lc_savings = [d["savings"]["lc_saved_pct"] for d in data]
delay_savings = [d["savings"]["delay_saved_pct"] for d in data]
pwr_savings = [d["savings"]["dyn_pwr_saved_pct"] for d in data]
tot_pwr_savings = [d["savings"]["tot_pwr_saved_pct"] for d in data]
sqnr_vals = [d["errors"]["sqnr_db"] for d in data]

x = np.arange(len(bench_names))
width = 0.38

# ==============================================================================
# Figure 1: Hardware Savings Breakdown (LUTs & Logic Cells)
# ==============================================================================
fig, ax = plt.subplots(figsize=(10, 4.8), dpi=300)
rects1 = ax.bar(x - width/2, lut_savings, width, label="LUT Reduction (%)", color="#1f77b4", edgecolor="black", linewidth=0.8)
rects2 = ax.bar(x + width/2, lc_savings, width, label="Logic Cell Reduction (%)", color="#2ca02c", edgecolor="black", linewidth=0.8)

ax.set_ylabel("Hardware Savings (%)", fontsize=11, fontweight="bold")
ax.set_title("pyALS Logic Synthesis Hardware Savings on AMD/Xilinx 7-Series FPGA (Yosys Flow)", fontsize=12, fontweight="bold", pad=12)
ax.set_xticks(x)
ax.set_xticklabels(bench_names, rotation=38, ha="right", fontsize=9, fontweight="semibold")
ax.legend(frameon=True, facecolor="white", edgecolor="#cccccc", fontsize=10, loc="upper right")
y_min1 = min(0.0, min(lut_savings), min(lc_savings))
y_max1 = max(max(lut_savings), max(lc_savings))
ax.set_ylim(y_min1 * 1.2 if y_min1 < 0 else 0, max(10.0, y_max1 * 1.18))
ax.grid(axis="y", linestyle="--", alpha=0.6)

for rect in rects1:
    height = rect.get_height()
    if abs(height) > 0.1:
        va = "bottom" if height >= 0 else "top"
        y_text = 3 if height >= 0 else -10
        ax.annotate(f"{height:.1f}%",
                    xy=(rect.get_x() + rect.get_width() / 2, height),
                    xytext=(0, y_text), textcoords="offset points",
                    ha="center", va=va, fontsize=7.5, rotation=90)

for rect in rects2:
    height = rect.get_height()
    if abs(height) > 0.1:
        va = "bottom" if height >= 0 else "top"
        y_text = 3 if height >= 0 else -10
        ax.annotate(f"{height:.1f}%",
                    xy=(rect.get_x() + rect.get_width() / 2, height),
                    xytext=(0, y_text), textcoords="offset points",
                    ha="center", va=va, fontsize=7.5, rotation=90, color="#1b6d1b")

plt.tight_layout()
fig1_path = FIGURES_DIR / "pyals_yosys_hardware_savings.png"
plt.savefig(fig1_path, dpi=300)
shutil.copy(fig1_path, ARTIFACT_DIR / "pyals_yosys_hardware_savings.png")
plt.close()
print(f"Saved: {fig1_path}")

# ==============================================================================
# Figure 2: Timing & Power Savings (Critical Delay & Dynamic Power)
# ==============================================================================
fig, ax = plt.subplots(figsize=(10, 4.8), dpi=300)
rects1 = ax.bar(x - width/2, delay_savings, width, label="Critical Path Delay Reduction (%)", color="#ff7f0e", edgecolor="black", linewidth=0.8)
rects2 = ax.bar(x + width/2, pwr_savings, width, label="Dynamic Power Reduction (%)", color="#9467bd", edgecolor="black", linewidth=0.8)

ax.set_ylabel("Improvement (%)", fontsize=11, fontweight="bold")
ax.set_title("pyALS FPGA Timing and Dynamic Power Savings (Yosys xc7 Flow)", fontsize=12, fontweight="bold", pad=12)
ax.set_xticks(x)
ax.set_xticklabels(bench_names, rotation=38, ha="right", fontsize=9, fontweight="semibold")
ax.legend(frameon=True, facecolor="white", edgecolor="#cccccc", fontsize=10, loc="upper right")
y_min2 = min(0.0, min(delay_savings), min(pwr_savings))
y_max2 = max(max(delay_savings), max(pwr_savings))
ax.set_ylim(y_min2 * 1.2 if y_min2 < 0 else 0, max(10.0, y_max2 * 1.18))
ax.grid(axis="y", linestyle="--", alpha=0.6)

for rect in rects1:
    height = rect.get_height()
    if abs(height) > 0.1:
        va = "bottom" if height >= 0 else "top"
        y_text = 3 if height >= 0 else -10
        ax.annotate(f"{height:.1f}%",
                    xy=(rect.get_x() + rect.get_width() / 2, height),
                    xytext=(0, y_text), textcoords="offset points",
                    ha="center", va=va, fontsize=7.5, rotation=90)

for rect in rects2:
    height = rect.get_height()
    if abs(height) > 0.1:
        va = "bottom" if height >= 0 else "top"
        y_text = 3 if height >= 0 else -10
        ax.annotate(f"{height:.1f}%",
                    xy=(rect.get_x() + rect.get_width() / 2, height),
                    xytext=(0, y_text), textcoords="offset points",
                    ha="center", va=va, fontsize=7.5, rotation=90, color="#5c357f")

plt.tight_layout()
fig2_path = FIGURES_DIR / "pyals_yosys_timing_power.png"
plt.savefig(fig2_path, dpi=300)
shutil.copy(fig2_path, ARTIFACT_DIR / "pyals_yosys_timing_power.png")
plt.close()
print(f"Saved: {fig2_path}")

# ==============================================================================
# Figure 3: Fidelity & Accuracy Trade-off (SQNR vs Logic Cell Savings)
# ==============================================================================
fig, ax = plt.subplots(figsize=(8.5, 5.0), dpi=300)
colors = ["#1f77b4" if d["fidelity"]["pass"] else "#d62728" for d in data]

scatter = ax.scatter(sqnr_vals, lc_savings, s=140, c=colors, alpha=0.85, edgecolors="black", linewidth=1.2, zorder=5)

for i, txt in enumerate(bench_names):
    dx = 0.5
    dy = 1.0 if (i % 2 == 0) else -2.5
    ax.annotate(txt, (sqnr_vals[i], lc_savings[i]), xytext=(dx, dy), textcoords="offset points", fontsize=8.5, fontweight="semibold")

ax.axvline(x=30.0, color="#d62728", linestyle="--", linewidth=1.5, label="High Fidelity Threshold (30.0 dB)", alpha=0.7)
ax.set_xlabel("Signal-to-Quantization-Noise Ratio / SQNR (dB)", fontsize=11, fontweight="bold")
ax.set_ylabel("Logic Cell Savings (%)", fontsize=11, fontweight="bold")
ax.set_title("pyALS Error-Energy Trade-off Space across AxBench Workloads", fontsize=12, fontweight="bold", pad=12)
ax.legend(frameon=True, facecolor="white", edgecolor="#cccccc", fontsize=9.5, loc="upper right")
ax.grid(True, linestyle="--", alpha=0.5)

plt.tight_layout()
fig3_path = FIGURES_DIR / "pyals_yosys_fidelity_tradeoff.png"
plt.savefig(fig3_path, dpi=300)
shutil.copy(fig3_path, ARTIFACT_DIR / "pyals_yosys_fidelity_tradeoff.png")
plt.close()
print(f"Saved: {fig3_path}")
print("All publication figures successfully generated!")
