"""Generate publication-grade figures for the AxBench FPGA benchmark evaluation suite.

Outputs:
1. reports/figures/axbench_fpga_hardware_savings.png
2. reports/figures/axbench_fpga_timing_power.png
3. reports/figures/axbench_fpga_fidelity_tradeoff.png
"""

from __future__ import annotations

import json
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

REPORTS_DIR = Path("/home/christos-ntoumas/AxFPGA/reports")
FIGURES_DIR = REPORTS_DIR / "figures"
FIGURES_DIR.mkdir(parents=True, exist_ok=True)

RESULTS_JSON = REPORTS_DIR / "axbench_fpga_benchmark_results.json"


def generate_plots():
    if not RESULTS_JSON.exists():
        print(f"Results JSON not found at {RESULTS_JSON}")
        return

    data = json.loads(RESULTS_JSON.read_text(encoding="utf-8"))
    plt.style.use(["science", "nature", "no-latex"])
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.size"] = 8

    # Extract data
    names = [d["name"] for d in data]
    lut_savings = [d["savings"]["lut_saved_pct"] for d in data]
    lc_savings = [d["savings"]["lc_saved_pct"] for d in data]
    carry_savings = [d["savings"]["carry_saved_pct"] for d in data]
    delay_savings = [d["savings"]["delay_saved_pct"] for d in data]
    pwr_savings = [d["savings"]["pwr_saved_pct"] for d in data]
    sqnr_vals = [d["errors"]["sqnr_db"] for d in data]

    x = np.arange(len(names))
    width = 0.35

    # -------------------------------------------------------------
    # Figure 1: Hardware Resource Reductions (LUT & Logic Cells)
    # -------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 4), dpi=300)
    bars1 = ax.bar(x - width/2, lut_savings, width, label="Slice LUT Reduction (%)", color="#1f77b4", edgecolor="black", linewidth=0.5)
    bars2 = ax.bar(x + width/2, lc_savings, width, label="Logic Cell (LC) Reduction (%)", color="#2ca02c", edgecolor="black", linewidth=0.5)

    ax.set_ylabel("Physical Hardware Savings (%)", fontweight="bold")
    ax.set_title("AMD Vivado 2026.1 Hardware Savings on Xilinx Artix-7 (AxBench Suite)", fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=35, ha="right", fontsize=8)
    ax.set_ylim(0, max(max(lut_savings), max(lc_savings)) * 1.25)
    ax.legend(frameon=True, loc="upper right")
    ax.grid(axis="y", linestyle="--", alpha=0.5)

    # Label values on bars
    for bar in bars1:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{h:.1f}%", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2),
                        textcoords="offset points", ha="center", va="bottom", fontsize=6.5)

    for bar in bars2:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{h:.1f}%", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2),
                        textcoords="offset points", ha="center", va="bottom", fontsize=6.5, color="#005500")

    fig.tight_layout()
    fig1_path = FIGURES_DIR / "axbench_fpga_hardware_savings.png"
    fig.savefig(fig1_path, dpi=300)
    plt.close(fig)
    print(f"Saved Figure 1 to {fig1_path}")

    # -------------------------------------------------------------
    # Figure 2: Timing Speedup & Power Savings
    # -------------------------------------------------------------
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4), dpi=300)

    # Delay / Speedup
    bars_delay = ax1.bar(x, delay_savings, width=0.5, color="#ff7f0e", edgecolor="black", linewidth=0.5)
    ax1.set_ylabel("Critical Path Delay Reduction (%)", fontweight="bold")
    ax1.set_title("Timing Improvement (Fmax Speedup)", fontweight="bold")
    ax1.set_xticks(x)
    ax1.set_xticklabels(names, rotation=45, ha="right", fontsize=7.5)
    ax1.set_ylim(0, max(delay_savings) * 1.3)
    ax1.grid(axis="y", linestyle="--", alpha=0.5)
    for bar in bars_delay:
        h = bar.get_height()
        if h > 0:
            ax1.annotate(f"{h:.1f}%", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2),
                         textcoords="offset points", ha="center", va="bottom", fontsize=6.5)

    # Power Savings
    bars_pwr = ax2.bar(x, pwr_savings, width=0.5, color="#d62728", edgecolor="black", linewidth=0.5)
    ax2.set_ylabel("Total On-Chip Power Reduction (%)", fontweight="bold")
    ax2.set_title("Physical Power Dissipation Savings", fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels(names, rotation=45, ha="right", fontsize=7.5)
    ax2.set_ylim(0, max(pwr_savings) * 1.35)
    ax2.grid(axis="y", linestyle="--", alpha=0.5)
    for bar in bars_pwr:
        h = bar.get_height()
        if h > 0:
            ax2.annotate(f"{h:.1f}%", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2),
                         textcoords="offset points", ha="center", va="bottom", fontsize=6.5)

    fig.tight_layout()
    fig2_path = FIGURES_DIR / "axbench_fpga_timing_power.png"
    fig.savefig(fig2_path, dpi=300)
    plt.close(fig)
    print(f"Saved Figure 2 to {fig2_path}")

    # -------------------------------------------------------------
    # Figure 3: Hardware Savings vs Signal Fidelity (SQNR)
    # -------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=300)
    scatter = ax.scatter(sqnr_vals, lc_savings, s=80, c=delay_savings, cmap="viridis", edgecolor="black", linewidth=0.8, alpha=0.9)
    cbar = plt.colorbar(scatter, ax=ax)
    cbar.set_label("Delay Reduction (%)", fontweight="bold")

    for i, name in enumerate(names):
        ax.annotate(name, (sqnr_vals[i], lc_savings[i]), xytext=(4, 2), textcoords="offset points", fontsize=7)

    ax.set_xlabel("Signal-to-Quantization-Noise Ratio (SQNR in dB)", fontweight="bold")
    ax.set_ylabel("Logic Cell (LC) Savings (%)", fontweight="bold")
    ax.set_title("Pareto Trade-Off: Hardware Savings vs. Arithmetic Fidelity", fontweight="bold")
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.set_xlim(min(sqnr_vals) - 3, max(sqnr_vals) + 3)
    ax.set_ylim(min(lc_savings) - 2, max(lc_savings) + 4)

    fig.tight_layout()
    fig3_path = FIGURES_DIR / "axbench_fpga_fidelity_tradeoff.png"
    fig.savefig(fig3_path, dpi=300)
    plt.close(fig)
    print(f"Saved Figure 3 to {fig3_path}")


if __name__ == "__main__":
    generate_plots()
