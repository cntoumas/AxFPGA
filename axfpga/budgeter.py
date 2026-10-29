"""Cascaded Multi-Cone Budgeting Engine for AxSYS (Phase 3).

Implements iterative error budget tightening loop for multi-stage pipelines:
    B_i^(k+1) = B_i^(k) * (B_top / E_top) * 0.9
until top-level error E_top <= B_top.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from axfpga.flow import AxcFlow

logger = logging.getLogger("axfpga.budgeter")


class CascadedBudgeter:
    """Iterative budget tightening loop for cascaded multi-cone / multi-stage pipelines."""

    def __init__(
        self,
        input_verilog: str | Path,
        output_dir: str | Path,
        b_top: float,
        metric: str = "med",
        initial_budgets: Optional[Dict[str, float]] = None,
        max_iterations: int = 10,
        min_budget: float = 0.01,
        contraction_factor: float = 0.9,
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
        self.b_top = float(b_top)
        self.metric = metric
        self.initial_budgets = dict(initial_budgets) if initial_budgets else None
        self.max_iterations = max_iterations
        self.min_budget = min_budget
        self.contraction_factor = contraction_factor
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

    def optimize_budgets(self) -> Dict[str, Any]:
        """Execute the cascaded multi-cone budget tightening loop."""
        logger.info("=== STARTING CASCADED MULTI-CONE BUDGETING ===")
        logger.info("Top-Level Error Bound B_top: %.4f (%s)", self.b_top, self.metric.upper())
        logger.info("Max Iterations: %d | Contraction Safety Factor: %.2f", self.max_iterations, self.contraction_factor)

        t_start = time.time()
        trajectory: List[Dict[str, Any]] = []

        # Initialize current budget for cones
        current_cone_budget = self.b_top
        if self.initial_budgets:
            current_cone_budget = list(self.initial_budgets.values())[0]

        converged = False
        final_summary: Optional[Dict[str, Any]] = None
        best_run_dir: Optional[Path] = None

        for k in range(self.max_iterations):
            iter_dir = self.output_dir / f"iter_{k}"
            iter_dir.mkdir(parents=True, exist_ok=True)

            logger.info("--- [Iteration %d/%d] Testing Cone Budget B_i = %.4f ---", k, self.max_iterations, current_cone_budget)
            iter_start = time.time()

            try:
                flow = AxcFlow(
                    input_verilog=self.input_verilog,
                    output_dir=iter_dir,
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
                    override_bound=current_cone_budget,
                    target=self.target,
                    fpga_family=self.fpga_family,
                    fpga_device=self.fpga_device,
                    fpga_nodsp=self.fpga_nodsp,
                )
                flow_res = flow.run()
                iter_elapsed = time.time() - iter_start

                # Compound top-level measured error
                approx_info = flow_res.get("approximation", {})
                e_top = float(approx_info.get("measured_error") or 0.0)
                approx_area = float(approx_info.get("approx_area") or 0.0)
                savings = float(approx_info.get("area_savings_percent") or 0.0)

                # Check convergence condition: E_top <= B_top
                is_compliant = (e_top <= self.b_top)
                ratio = 1.0

                step_record = {
                    "iteration": k,
                    "cone_budget": current_cone_budget,
                    "e_top": e_top,
                    "b_top": self.b_top,
                    "converged": is_compliant,
                    "approx_area": approx_area,
                    "area_savings_percent": savings,
                    "elapsed_seconds": round(iter_elapsed, 2),
                }

                if is_compliant:
                    logger.info("Iter %d: CONVERGENCE ACHIEVED! E_top = %.4f <= B_top = %.4f", k, e_top, self.b_top)
                    step_record["contraction_ratio"] = 1.0
                    trajectory.append(step_record)
                    converged = True
                    final_summary = flow_res
                    best_run_dir = iter_dir
                    break
                else:
                    # Update formula: B_i <- B_i * (B_top / E_top) * 0.9
                    if e_top > 0:
                        ratio = (self.b_top / e_top) * self.contraction_factor
                    else:
                        ratio = self.contraction_factor

                    new_budget = max(current_cone_budget * ratio, self.min_budget)
                    logger.info(
                        "Iter %d: Exceeded budget (E_top=%.4f > B_top=%.4f). Contraction ratio=%.4f. Tightening B_i: %.4f -> %.4f",
                        k, e_top, self.b_top, ratio, current_cone_budget, new_budget
                    )
                    step_record["contraction_ratio"] = round(ratio, 4)
                    trajectory.append(step_record)
                    final_summary = flow_res
                    best_run_dir = iter_dir
                    current_cone_budget = new_budget

            except Exception as exc:
                logger.error("Iter %d encountered error: %s", k, exc)
                trajectory.append({
                    "iteration": k,
                    "cone_budget": current_cone_budget,
                    "e_top": 999999.0,
                    "b_top": self.b_top,
                    "converged": False,
                    "contraction_ratio": self.contraction_factor,
                    "error": str(exc),
                })
                current_cone_budget = max(current_cone_budget * self.contraction_factor, self.min_budget)

        total_elapsed = time.time() - t_start

        # 1. Export Trajectory JSON
        traj_path = self.output_dir / "budget_trajectory.json"
        with open(traj_path, "w", encoding="utf-8") as f:
            json.dump({
                "status": "SUCCESS" if converged else "MAX_ITERATIONS_REACHED",
                "converged": converged,
                "b_top": self.b_top,
                "metric": self.metric,
                "total_iterations": len(trajectory),
                "total_elapsed_seconds": round(total_elapsed, 2),
                "trajectory": trajectory,
            }, f, indent=2)

        # 2. Export Summary CSV
        csv_path = self.output_dir / "budget_summary.csv"
        fieldnames = ["iteration", "cone_budget", "e_top", "b_top", "converged", "approx_area", "area_savings_percent", "contraction_ratio", "elapsed_seconds"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in trajectory:
                writer.writerow(row)

        logger.info("=== BUDGETING FINISHED: converged=%s in %d iterations (%.2fs) ===", converged, len(trajectory), total_elapsed)
        logger.info("Trajectory JSON: %s", traj_path)
        logger.info("Summary CSV: %s", csv_path)

        return {
            "status": "SUCCESS" if converged else "MAX_ITERATIONS_REACHED",
            "converged": converged,
            "iterations_run": len(trajectory),
            "final_e_top": trajectory[-1].get("e_top") if trajectory else 0.0,
            "b_top": self.b_top,
            "final_budget": current_cone_budget,
            "trajectory": trajectory,
            "best_run_dir": str(best_run_dir) if best_run_dir else None,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="AxSYS Cascaded Multi-Cone Budgeting CLI")
    parser.add_argument("-i", "--input", required=True, help="Input annotated Verilog file")
    parser.add_argument("-o", "--output-dir", default="build/budget_run", help="Output directory for budgeting artifacts")
    parser.add_argument("--b-top", type=float, required=True, help="Top-level error bound B_top")
    parser.add_argument("-m", "--metric", default="med", help="Target error metric (med, mred, wce, er)")
    parser.add_argument("--max-iter", type=int, default=8, help="Maximum budget tightening iterations")
    parser.add_argument("--min-budget", type=float, default=0.01, help="Minimum allowable per-cone budget")
    parser.add_argument("--contraction-factor", type=float, default=0.9, help="Safety contraction factor (default 0.9)")
    parser.add_argument("-t", "--tech", default="NanGate15nm", help="Standard cell tech library")
    parser.add_argument("--target", default="asic", choices=["asic", "fpga"], help="Target architecture (asic, fpga)")
    parser.add_argument("--fpga-family", default="xc7", help="Target FPGA family (default: xc7)")
    parser.add_argument("--fpga-device", default="xc7a35t", help="Target FPGA device (default: xc7a35t)")
    parser.add_argument("--nodsp", action="store_true", help="Disable DSP slice inference on FPGA (default: false)")
    parser.add_argument("--allow-dsp", action="store_true", default=True, help="Allow DSP slice inference on FPGA (default: true)")
    parser.add_argument("--clk", dest="clk", default="clk", help="Clock port name")
    parser.add_argument("--period", dest="period", type=float, default=10.0, help="Clock period in ns")
    parser.add_argument("-s", "--samples", type=int, default=200, help="AxLS simulation samples")
    parser.add_argument("-n", "--verify-cycles", type=int, default=200, help="Verification clock cycles")
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

    budgeter = CascadedBudgeter(
        input_verilog=args.input,
        output_dir=args.output_dir,
        b_top=args.b_top,
        metric=args.metric,
        max_iterations=args.max_iter,
        min_budget=args.min_budget,
        contraction_factor=args.contraction_factor,
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
    result = budgeter.optimize_budgets()
    print(json.dumps({
        "status": result["status"],
        "converged": result["converged"],
        "iterations_run": result["iterations_run"],
        "final_e_top": result["final_e_top"],
        "b_top": result["b_top"],
        "final_budget": result["final_budget"],
    }, indent=2))


if __name__ == "__main__":
    main()
