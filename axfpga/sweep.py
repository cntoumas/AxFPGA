"""Pareto Sweeper CLI and API for AxSYS (Phase 3).

Executes automated sweeps over comma-separated error bounds, collects standard-cell
area, power, timing, and formal SAT verification metrics, and generates publication-quality
Pareto trade-off plots and CSV summaries.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from axfpga.flow import AxcFlow

logger = logging.getLogger("axfpga.sweep")


class ParetoSweeper:
    """Automated Pareto Sweeps over error bounds."""

    def __init__(
        self,
        input_verilog: str | Path,
        output_dir: str | Path,
        metric: str = "med",
        method: str = "auto",
        tech: str = "NanGate15nm",
        clk_port: str = "clk",
        clk_period_ns: float = 10.0,
        sim_samples: int = 300,
        verify_cycles: int = 300,
        decompose_operators: bool = False,
        use_realistic_traces: bool = True,
        stimulus_file: Optional[str | Path] = None,
        enable_formal: bool = True,
        formal_timeout: int = 60,
        target: str = "asic",
        fpga_family: str = "xc7",
        fpga_device: str = "xc7a35t",
        fpga_nodsp: bool = False,
    ):
        self.input_verilog = Path(input_verilog).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.metric = metric
        self.method = method
        self.tech = tech
        self.clk_port = clk_port
        self.clk_period_ns = clk_period_ns
        self.sim_samples = sim_samples
        self.verify_cycles = verify_cycles
        self.decompose_operators = decompose_operators
        self.use_realistic_traces = use_realistic_traces
        self.stimulus_file = Path(stimulus_file).resolve() if stimulus_file else None
        self.enable_formal = enable_formal
        self.formal_timeout = formal_timeout
        self.target = target.lower()
        self.fpga_family = fpga_family.lower()
        self.fpga_device = fpga_device.lower()
        self.fpga_nodsp = fpga_nodsp

        if self.tech.lower() in ("xc7", "artix7", "xilinx"):
            self.target = "fpga"
            self.fpga_family = "xc7"

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def parse_bounds(self, bounds_input: Union[str, List[float], List[int]]) -> List[float]:
        """Parse comma-separated string or sequence into sorted list of unique float bounds."""
        if isinstance(bounds_input, str):
            tokens = [t.strip() for t in bounds_input.split(",") if t.strip()]
            parsed = [float(t) for t in tokens]
        else:
            parsed = [float(b) for b in bounds_input]
        return sorted(list(dict.fromkeys(parsed)))

    def run_sweep(self, bounds: Union[str, List[float], List[int]]) -> Dict[str, Any]:
        """Execute sweep across all specified bounds."""
        parsed_bounds = self.parse_bounds(bounds)
        if not parsed_bounds:
            raise ValueError("No valid bounds specified for sweep.")

        logger.info("=== STARTING PARETO SWEEP ===")
        logger.info("Input Verilog: %s", self.input_verilog)
        logger.info("Output Directory: %s", self.output_dir)
        logger.info("Target Bounds: %s", parsed_bounds)
        logger.info("Approximation Method: %s", self.method)

        t_sweep_start = time.time()
        results: List[Dict[str, Any]] = []

        for b_idx, bound in enumerate(parsed_bounds):
            run_tag = f"bound_{bound:g}".replace(".", "_")
            run_dir = self.output_dir / run_tag
            run_dir.mkdir(parents=True, exist_ok=True)

            logger.info("--- [Point %d/%d] Running Flow with Target Bound: %.4f ---", b_idx + 1, len(parsed_bounds), bound)
            point_start = time.time()

            try:
                flow = AxcFlow(
                    input_verilog=self.input_verilog,
                    output_dir=run_dir,
                    tech=self.tech,
                    clk_port=self.clk_port,
                    clk_period_ns=self.clk_period_ns,
                    sim_samples=self.sim_samples,
                    verify_cycles=self.verify_cycles,
                    decompose_operators=self.decompose_operators,
                    use_realistic_traces=self.use_realistic_traces,
                    stimulus_file=self.stimulus_file,
                    enable_formal=self.enable_formal,
                    formal_timeout=self.formal_timeout,
                    axc_method=self.method,
                    override_bound=bound,
                    target=self.target,
                    fpga_family=self.fpga_family,
                    fpga_device=self.fpga_device,
                    fpga_nodsp=self.fpga_nodsp,
                )
                flow_summary = flow.run()
                point_elapsed = time.time() - point_start

                approx = flow_summary.get("approximation", {})
                power = flow_summary.get("power", {})
                fpga = flow_summary.get("fpga", {})
                formal_ctrl = flow_summary.get("formal_control_status", "DISABLED")
                formal_wce = flow_summary.get("formal_wce_status", "DISABLED")

                row = {
                    "bound": bound,
                    "target": self.target,
                    "flow_status": flow_summary.get("flow_status", "UNKNOWN"),
                    "metric": self.metric,
                    "measured_error": approx.get("measured_error", 0.0),
                    "champion_method": approx.get("champion_method", self.method),
                    "initial_area": approx.get("initial_area", 0.0),
                    "approx_area": approx.get("approx_area", 0.0),
                    "area_savings_percent": approx.get("area_savings_percent", 0.0),
                    "initial_power_uw": power.get("total_power_uw", 0.0),
                    "approx_power_uw": power.get("total_power_uw", 0.0),
                    "power_savings_percent": power.get("power_savings_percent", 0.0),
                    "total_luts": fpga.get("approx", {}).get("total_luts", 0),
                    "luts_saved": fpga.get("luts_saved", 0),
                    "lut_reduction_pct": fpga.get("lut_reduction_pct", 0.0),
                    "carry4": fpga.get("approx", {}).get("carry4", 0),
                    "estimated_lcs": fpga.get("approx", {}).get("estimated_lcs", 0),
                    "lc_reduction_pct": fpga.get("lc_reduction_pct", 0.0),
                    "control_sat_status": formal_ctrl,
                    "wce_sat_status": formal_wce,
                    "elapsed_seconds": round(point_elapsed, 2),
                    "run_dir": str(run_dir),
                }
                results.append(row)
                logger.info(
                    "Point %d completed: error=%.4f, area=%.2f (%.2f%% savings), status=%s",
                    b_idx + 1, row["measured_error"], row["approx_area"], row["area_savings_percent"], row["flow_status"]
                )

            except Exception as exc:
                logger.error("Failed point bound=%.4f: %s", bound, exc)
                results.append({
                    "bound": bound,
                    "flow_status": "FAILED",
                    "metric": self.metric,
                    "measured_error": None,
                    "champion_method": None,
                    "initial_area": 0.0,
                    "approx_area": 0.0,
                    "area_savings_percent": 0.0,
                    "initial_power_uw": 0.0,
                    "approx_power_uw": 0.0,
                    "power_savings_percent": 0.0,
                    "control_sat_status": "ERROR",
                    "wce_sat_status": "ERROR",
                    "elapsed_seconds": round(time.time() - point_start, 2),
                    "run_dir": str(run_dir),
                    "error": str(exc),
                })

        total_elapsed = time.time() - t_sweep_start

        # 1. Export CSV
        csv_path = self.output_dir / "pareto_results.csv"
        self._export_csv(results, csv_path)

        # 2. Export JSON
        summary_payload = {
            "sweep_status": "SUCCESS",
            "input_verilog": str(self.input_verilog),
            "tech": self.tech,
            "metric": self.metric,
            "method": self.method,
            "total_points": len(parsed_bounds),
            "total_elapsed_seconds": round(total_elapsed, 2),
            "results": results,
        }
        json_path = self.output_dir / "pareto_summary.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary_payload, f, indent=2)

        # 3. Generate Trade-Off Plots
        plot_path = self.output_dir / "pareto_curve.png"
        self._generate_plots(results, plot_path)

        logger.info("=== PARETO SWEEP FINISHED IN %.2f SECONDS ===", total_elapsed)
        logger.info("CSV Summary: %s", csv_path)
        logger.info("JSON Summary: %s", json_path)
        logger.info("Pareto Plot: %s", plot_path)

        return summary_payload

    def _export_csv(self, results: List[Dict[str, Any]], out_path: Path) -> None:
        """Write tabular Pareto sweep results to CSV."""
        if not results:
            return
        fieldnames = [
            "bound", "flow_status", "metric", "measured_error", "champion_method",
            "initial_area", "approx_area", "area_savings_percent",
            "initial_power_uw", "approx_power_uw", "power_savings_percent",
            "control_sat_status", "wce_sat_status", "elapsed_seconds", "run_dir"
        ]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in results:
                writer.writerow(r)

    def _generate_plots(self, results: List[Dict[str, Any]], out_path: Path) -> None:
        """Generate publication-grade 4-panel Pareto trade-off curve figure."""
        valid_pts = [r for r in results if r.get("flow_status") in ("SUCCESS", "VERIFICATION_FAILED") and r.get("approx_area", 0) > 0]
        if not valid_pts:
            logger.warning("No valid points to plot.")
            return

        bounds = [r["bound"] for r in valid_pts]
        areas = [r["approx_area"] for r in valid_pts]
        savings = [r["area_savings_percent"] for r in valid_pts]
        init_area = valid_pts[0].get("initial_area", areas[0])
        errors = [r.get("measured_error", 0.0) for r in valid_pts]

        fig, axs = plt.subplots(2, 2, figsize=(12, 9))
        plt.subplots_adjust(hspace=0.35, wspace=0.3)

        # Panel 1: Gate Area vs Target Error Bound
        ax1 = axs[0, 0]
        ax1.plot(bounds, areas, "o-", color="#1f77b4", linewidth=2.5, markersize=8, label="Approx Netlist")
        ax1.axhline(init_area, color="#d62728", linestyle="--", linewidth=1.8, label=f"Exact ({init_area:.1f} $\\mu m^2$)")
        ax1.set_xlabel("Target Error Bound", fontsize=11, fontweight="bold")
        ax1.set_ylabel("Standard-Cell Area ($\\mu m^2$)", fontsize=11, fontweight="bold")
        ax1.set_title("Standard-Cell Area vs Target Bound", fontsize=12, fontweight="bold")
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(loc="best", frameon=True)

        # Panel 2: Area Savings (%) vs Error Bound
        ax2 = axs[0, 1]
        ax2.plot(bounds, savings, "s-", color="#2ca02c", linewidth=2.5, markersize=8)
        ax2.set_xlabel("Target Error Bound", fontsize=11, fontweight="bold")
        ax2.set_ylabel("Area Savings (%)", fontsize=11, fontweight="bold")
        ax2.set_title("Area Savings vs Target Bound", fontsize=12, fontweight="bold")
        ax2.grid(True, linestyle=":", alpha=0.6)

        # Panel 3: Measured Error vs Target Error Bound
        ax3 = axs[1, 0]
        ax3.plot(bounds, errors, "^-", color="#ff7f0e", linewidth=2.5, markersize=8, label="Simulated Error")
        ax3.plot(bounds, bounds, ":", color="gray", label="Ideal Upper Limit (y = x)")
        ax3.set_xlabel("Target Error Bound", fontsize=11, fontweight="bold")
        ax3.set_ylabel(f"Measured {self.metric.upper()} Error", fontsize=11, fontweight="bold")
        ax3.set_title("Fidelity Adherence (Simulated vs Target)", fontsize=12, fontweight="bold")
        ax3.grid(True, linestyle=":", alpha=0.6)
        ax3.legend(loc="best", frameon=True)

        # Panel 4: Formal Verification Status & Champion Method per Point
        ax4 = axs[1, 1]
        y_pos = list(range(len(valid_pts)))
        labels = [f"B={r['bound']:g} [{r.get('champion_method', 'auto')}]" for r in valid_pts]
        ctrl_colors = ["#2ca02c" if r.get("control_sat_status") == "PROVEN" else "#d62728" for r in valid_pts]
        bars = ax4.barh(y_pos, savings, color=ctrl_colors, alpha=0.85, edgecolor="black")
        ax4.set_yticks(y_pos)
        ax4.set_yticklabels(labels, fontsize=10)
        ax4.set_xlabel("Area Savings (%)", fontsize=11, fontweight="bold")
        ax4.set_title("Champion Method & Formal Control Status\n(Green = 100% Control SAT Proven)", fontsize=11, fontweight="bold")
        ax4.grid(True, linestyle=":", alpha=0.6, axis="x")

        fig.suptitle(f"AxSYS Pareto Frontier Analysis ({self.tech} Library)", fontsize=14, fontweight="bold")
        plt.tight_layout()
        plt.savefig(out_path, dpi=200)
        plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="AxSYS Pareto Frontier Sweeper CLI")
    parser.add_argument("-i", "--input", required=True, help="Input annotated Verilog file")
    parser.add_argument("-o", "--output-dir", default="build/pareto_sweep", help="Output directory for sweep artifacts")
    parser.add_argument("--bounds", required=True, help="Comma-separated target error bounds (e.g. 1.0,2.0,4.0,8.0)")
    parser.add_argument("-m", "--metric", default="med", help="Target error metric (med, mred, wce, er)")
    parser.add_argument("-a", "--method", default="auto", choices=["auto", "inouts", "probprun", "decisiontree"], help="Logic approximation backend algorithm")
    parser.add_argument("-t", "--tech", default="NanGate15nm", help="Standard cell tech library")
    parser.add_argument("--target", default="asic", choices=["asic", "fpga"], help="Target architecture (asic, fpga)")
    parser.add_argument("--fpga-family", default="xc7", help="Target FPGA family (default: xc7)")
    parser.add_argument("--fpga-device", default="xc7a35t", help="Target FPGA device (default: xc7a35t)")
    parser.add_argument("--nodsp", action="store_true", help="Disable DSP slice inference on FPGA (default: false)")
    parser.add_argument("--allow-dsp", action="store_true", default=True, help="Allow DSP slice inference on FPGA (default: true)")
    parser.add_argument("--clk", dest="clk", default="clk", help="Clock port name")
    parser.add_argument("--period", dest="period", type=float, default=10.0, help="Clock period in ns")
    parser.add_argument("-s", "--samples", type=int, default=300, help="Simulation samples")
    parser.add_argument("-n", "--verify-cycles", type=int, default=300, help="Verification clock cycles")
    parser.add_argument("--decompose", action="store_true", help="Decompose coarse-grain arithmetic cells")
    parser.add_argument("--no-trace", action="store_true", help="Disable realistic pipeline trace extraction")
    parser.add_argument("--stimulus-file", default=None, help="Primary input trace stimulus file")
    parser.add_argument("--no-formal", action="store_true", help="Disable Yosys SAT formal verification")
    parser.add_argument("--formal-timeout", type=int, default=60, help="Timeout in seconds for SAT solver")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sweeper = ParetoSweeper(
        input_verilog=args.input,
        output_dir=args.output_dir,
        metric=args.metric,
        method=args.method,
        tech=args.tech,
        clk_port=args.clk,
        clk_period_ns=args.period,
        sim_samples=args.samples,
        verify_cycles=args.verify_cycles,
        decompose_operators=args.decompose,
        use_realistic_traces=not args.no_trace,
        stimulus_file=args.stimulus_file,
        enable_formal=not args.no_formal,
        formal_timeout=args.formal_timeout,
        target=args.target,
        fpga_family=args.fpga_family,
        fpga_device=args.fpga_device,
        fpga_nodsp=args.nodsp,
    )
    result = sweeper.run_sweep(args.bounds)
    print(json.dumps({"sweep_status": result.get("sweep_status"), "total_points": result.get("total_points")}, indent=2))


if __name__ == "__main__":
    main()
