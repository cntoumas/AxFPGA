"""Top-Level Flow Orchestrator for Attribute-Driven Approximate Computing.

Coordinates the complete pipeline:
1. RTLIL Relaxability Analysis & Cone Extraction (Pyosys)
2. Approximate Logic Synthesis Optimization (AxLS)
3. Parent Shell & Approximated Cone Re-integration (Yosys)
4. Attribute-Driven SDC Timing Relaxation Generation
5. Automated Miter Verification (Icarus Verilog)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from axfpga.cone_extractor import ConeExtractor
from axfpga.estimator import FpgaEstimator
from axfpga.pyals_bridge import PyAlsBridge
from axfpga.reintegrator import Reintegrator
from axfpga.sdc_generator import SdcGenerator
from axfpga.trace_extractor import TraceExtractor
from axfpga.verifier import MiterVerifier

try:
    from axfpga.power_estimator import PowerEstimator
except ImportError:
    PowerEstimator = None

logger = logging.getLogger("axc_flow")


class AxcFlow:
    """Full-flow execution manager."""

    def __init__(
        self,
        input_verilog: str | Path,
        output_dir: str | Path,
        tech: str = "NanGate15nm",
        clk_port: str = "clk",
        clk_period_ns: float = 10.0,
        sim_samples: int = 500,
        verify_cycles: int = 500,
        decompose_operators: bool = False,
        use_realistic_traces: bool = True,
        stimulus_file: Optional[str | Path] = None,
        enable_formal: bool = True,
        formal_timeout: int = 60,
        axc_method: str = "auto",
        override_bound: Optional[float] = None,
        target: str = "fpga",
        fpga_family: str = "xc7",
        fpga_device: str = "xc7a35t",
        fpga_nodsp: bool = True,
    ):
        self.input_verilog = Path(input_verilog).resolve()
        self.output_dir = Path(output_dir).resolve()
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
        self.axc_method = axc_method
        self.override_bound = float(override_bound) if override_bound is not None else None
        self.target = target.lower()
        self.fpga_family = fpga_family.lower()
        self.fpga_device = fpga_device.lower()
        self.fpga_nodsp = fpga_nodsp

        # Auto-detect FPGA from tech string
        if self.tech.lower() in ("xc7", "artix7", "xilinx"):
            self.target = "fpga"
            self.fpga_family = "xc7"

        # Resolve tech Verilog library path
        tech_p = Path(self.tech)
        if tech_p.is_file():
            self.tech_vlog_path: Optional[Path] = tech_p.resolve()
        else:
            cand = Path(__file__).resolve().parent.parent / "third_party" / "AxLS" / "templates" / f"{self.tech}.v"
            self.tech_vlog_path = cand.resolve() if cand.is_file() else None

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> Dict[str, Any]:
        """Execute the end-to-end flow."""
        start_time = time.time()
        logger.info("=== STARTING AXC EDA FLOW ===")
        logger.info("Input Verilog: %s", self.input_verilog)
        logger.info("Output Directory: %s", self.output_dir)
        logger.info("Technology: %s (library file: %s)", self.tech, self.tech_vlog_path)

        # -------------------------------------------------------------
        # STEP 1: Cone Extraction
        # -------------------------------------------------------------
        current_input_vlog = self.input_verilog
        logger.info("--- Step 1: RTLIL Relaxability Analysis & Cone Extraction ---")
        submod_vlog = self.output_dir / "extracted_cone.v"
        top_shell_vlog = self.output_dir / "top_shell.v"
        meta_json_path = self.output_dir / "cone_meta.json"

        extractor = ConeExtractor(verilog_path=current_input_vlog, decompose_operators=self.decompose_operators)
        cone_result = extractor.extract_safe_cone(
            submod_tag="axc_cone_0",
            out_submod_path=submod_vlog,
            out_top_path=top_shell_vlog,
            out_meta_path=meta_json_path,
        )

        if cone_result.get("status") != "SUCCESS":
            elapsed = time.time() - start_time
            status_reason = cone_result.get("status", "EXTRACTION_FAILED")
            logger.warning("Cone extraction halted with status: %s", status_reason)
            stopped_summary: Dict[str, Any] = {
                "flow_status": "STOPPED",
                "reason": status_reason,
                "top_module": cone_result.get("top_module", "unknown"),
                "elapsed_seconds": round(elapsed, 2),
                "details": cone_result,
            }
            summary_path = self.output_dir / "flow_summary.json"
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(stopped_summary, f, indent=2)
            return stopped_summary

        relaxed_signals = cone_result.get("relaxed_signals", [])
        logger.info("Identified %d relaxed signal(s): %s", len(relaxed_signals), [s.get("wire") for s in relaxed_signals])
        for s in relaxed_signals:
            logger.info("  Signal: %s | Metric: %s | Bound: %s | LSB: %s",
                        s.get("wire"), s.get("metric"), s.get("bound"), s.get("lsb_width"))

        primary_metric = relaxed_signals[0].get("metric", "MED") if relaxed_signals else "MED"
        # When multiple relaxed signals exist, target the most restrictive (minimum) bound
        bounds_list = [float(s["bound"]) for s in relaxed_signals if "bound" in s]
        primary_bound = min(bounds_list) if bounds_list else 0.05
        if self.override_bound is not None:
            primary_bound = self.override_bound

        lsb_candidates = [int(s["lsb_width"]) for s in relaxed_signals if s.get("lsb_width") is not None]
        primary_lsb = max(lsb_candidates) if lsb_candidates else None

        # -------------------------------------------------------------
        # STEP 1.5: Realistic Pipeline Trace Extraction
        # -------------------------------------------------------------
        trace_hex_path = None
        if self.use_realistic_traces:
            logger.info("--- Step 1.5: Realistic Pipeline Trace Extraction ---")
            trace_cand = self.output_dir / "cone_trace.hex"
            try:
                trace_extractor = TraceExtractor(
                    top_shell_path=top_shell_vlog,
                    extracted_cone_path=submod_vlog,
                    meta_json=meta_json_path,
                    tech_vlog_path=self.tech_vlog_path,
                )
                trace_res = trace_extractor.extract_traces(
                    out_trace_hex=trace_cand,
                    num_samples=self.sim_samples,
                    stimulus_file=self.stimulus_file,
                    clk_period_ns=self.clk_period_ns,
                )
                if trace_res.get("status") == "SUCCESS":
                    trace_hex_path = trace_cand
                    logger.info("Captured %d realistic datapath vectors for AxLS training", trace_res.get("num_vectors", 0))
                else:
                    logger.warning("Trace extraction was not successful (%s); falling back to synthetic dataset", trace_res.get("status"))
            except Exception as exc:
                logger.warning("Trace extraction encountered error (%s); falling back to synthetic dataset", exc)

        # -------------------------------------------------------------
        # STEP 2: Logic Approximation (Native FPGA or pyALS)
        # -------------------------------------------------------------
        approx_cone_vlog = self.output_dir / "approx_cone.v"
        
        if self.target == "fpga" or self.axc_method != "pyals":
            logger.info("--- Step 2: Native FPGA Approximate Logic Synthesis ---")
            fpga_work_dir = self.output_dir / "fpga_workspace"
            from axfpga.backend import FpgaBackend

            backend = FpgaBackend(
                cone_verilog_path=submod_vlog,
                meta_json_path=meta_json_path,
                target_family=self.fpga_family,
                target_device=self.fpga_device,
                nodsp=self.fpga_nodsp,
                work_dir=fpga_work_dir,
                trace_hex_path=trace_hex_path,
                sim_samples=self.sim_samples,
            )
            approx_result = backend.optimize(
                metric=primary_metric,
                bound=primary_bound,
                lsb_width=primary_lsb,
                method=self.axc_method,
                out_approx_path=approx_cone_vlog,
            )
        else:
            logger.info("--- Step 2: pyALS Catalog-Based AIG-Rewriting Approximation ---")
            pyals_work_dir = self.output_dir / "pyals_workspace"
            bridge = PyAlsBridge(
                cone_verilog_path=submod_vlog,
                meta_json_path=meta_json_path,
                work_dir=pyals_work_dir,
                target_family=self.fpga_family,
                target_device=self.fpga_device,
                nodsp=self.fpga_nodsp,
                sim_samples=self.sim_samples,
            )

            approx_result = bridge.optimize(
                metric=primary_metric,
                bound=primary_bound,
                out_approx_path=approx_cone_vlog,
            )

        # -------------------------------------------------------------
        # STEP 3: Re-Integration
        # -------------------------------------------------------------
        logger.info("--- Step 3: Top Shell and Approximate Cone Re-integration ---")
        final_top_vlog = self.output_dir / "final_approx_top.v"

        tech_vlog_to_pass = None if self.target == "fpga" else self.tech_vlog_path
        reintegrator = Reintegrator(
            top_shell_path=top_shell_vlog,
            approx_cone_path=approx_cone_vlog,
            extracted_cone_path=submod_vlog,
            tech=self.tech,
            tech_verilog_path=tech_vlog_to_pass,
        )
        reint_result = reintegrator.reintegrate(out_final_path=final_top_vlog)

        # -------------------------------------------------------------
        # STEP 4: SDC Constraint Generation
        # -------------------------------------------------------------
        logger.info("--- Step 4: Attribute-Driven SDC Timing Constraint Generation ---")
        out_sdc_path = self.output_dir / "final_approx_top.sdc"

        sdc_gen = SdcGenerator(
            clk_port=self.clk_port,
            clk_period_ns=self.clk_period_ns,
            relaxation_factor=1.5,
            constraint_mode="max_delay",
        )
        sdc_gen.generate_sdc(metadata=cone_result, out_sdc_path=out_sdc_path)

        # -------------------------------------------------------------
        # STEP 5: Automated Verification
        # -------------------------------------------------------------
        logger.info("--- Step 5: Miter Verification Harness ---")
        verifier = MiterVerifier(
            exact_vlog=self.input_verilog,
            approx_vlog=final_top_vlog,
            meta_json=meta_json_path,
            tech=self.tech,
            tech_vlog=None if self.target == "fpga" else self.tech_vlog_path,
            num_cycles=self.verify_cycles,
            enable_formal=self.enable_formal,
            formal_timeout=self.formal_timeout,
            extracted_cone_vlog=submod_vlog,
            approx_cone_vlog=approx_cone_vlog,
        )
        verify_result = verifier.verify()

        # -------------------------------------------------------------
        # STEP 6: Target Evaluation (FPGA Resources or ASIC Power/Area)
        # -------------------------------------------------------------
        power_info: Dict[str, Any] = {}
        fpga_info: Dict[str, Any] = {}

        if self.target == "fpga":
            logger.info("--- Step 6: FPGA Resource Estimation & Utilization (%s / %s) ---", self.fpga_family, self.fpga_device)
            try:
                fpga_estimator = FpgaEstimator(
                    family=self.fpga_family,
                    device=self.fpga_device,
                    nodsp=self.fpga_nodsp,
                )
                fpga_info = fpga_estimator.compare_exact_vs_approx(
                    exact_netlist=self.input_verilog,
                    approx_netlist=final_top_vlog,
                    top_module=cone_result.get("top_module"),
                    work_dir=self.output_dir,
                )
                logger.info(
                    "FPGA Resource Savings [%s]: LUTs=%d (-%.2f%%) | CARRY4=%d (-%.2f%%) | Estimated LCs=%d (-%.2f%%)",
                    self.fpga_device,
                    fpga_info["luts_saved"],
                    fpga_info["lut_reduction_pct"],
                    fpga_info["carry_saved"],
                    fpga_info["carry_reduction_pct"],
                    fpga_info["lcs_saved"],
                    fpga_info["lc_reduction_pct"],
                )
            except Exception as exc:
                logger.warning("FPGA resource estimation could not be completed: %s", exc)
        else:
            logger.info("--- Step 6: Power Dissipation Estimation ---")
            try:
                clk_mhz = 1000.0 / self.clk_period_ns if self.clk_period_ns > 0 else 100.0
                power_estimator = PowerEstimator(tech=self.tech, clk_mhz=clk_mhz)
                power_info = power_estimator.evaluate_netlist(final_top_vlog)
                logger.info(
                    "Power dissipation: Total=%.3f uW (Leakage=%.3f uW, Dynamic=%.3f uW)",
                    power_info["total_power_uw"],
                    power_info["leakage_power_uw"],
                    power_info["dynamic_power_uw"],
                )
            except Exception as exc:
                logger.warning("Power estimation could not be completed: %s", exc)

        elapsed = time.time() - start_time
        logger.info("=== FLOW FINISHED IN %.2f SECONDS ===", elapsed)

        formal_verification = verify_result.get("formal_verification", {})

        summary: Dict[str, Any] = {
            "flow_status": "SUCCESS" if verify_result.get("status") == "PASSED" else "VERIFICATION_FAILED",
            "target": self.target,
            "elapsed_seconds": round(elapsed, 2),
            "top_module": cone_result.get("top_module", "unknown"),
            "submodule_module": cone_result.get("submodule_module", ""),
            "extracted_safe_cells": cone_result.get("num_safe_cells", 0),
            "trace_driven": {
                "enabled": self.use_realistic_traces and trace_hex_path is not None,
                "trace_file": str(trace_hex_path) if trace_hex_path else None,
            },
            "formal_control_status": formal_verification.get("control_equivalence", {}).get("status", "DISABLED"),
            "formal_wce_status": formal_verification.get("wce_bound", {}).get("status", "DISABLED"),
            "approximation": {
                "metric": primary_metric,
                "bound": primary_bound,
                "method": self.axc_method,
                "champion_method": approx_result.get("champion_method", self.axc_method),
                "leaderboard": approx_result.get("leaderboard", []),
                "relaxed_signals": relaxed_signals,
                "measured_error": approx_result.get("measured_error"),
                "pruned_nodes": approx_result.get("pruned_nodes_count"),
                "initial_area": approx_result.get("initial_area"),
                "approx_area": approx_result.get("final_area"),
                "area_savings_percent": approx_result.get("area_savings_percent"),
            },
            "power": power_info,
            "fpga": fpga_info,
            "verification": verify_result,
            "generated_files": {
                "top_shell": str(top_shell_vlog),
                "extracted_cone": str(submod_vlog),
                "cone_trace": str(trace_hex_path) if trace_hex_path else None,
                "approx_cone": str(approx_cone_vlog),
                "final_approx_top": str(final_top_vlog),
                "sdc_constraints": str(out_sdc_path),
                "metadata": str(meta_json_path),
            },
        }

        # Write run summary JSON
        summary_path = self.output_dir / "flow_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Full AxFPGA Approximate Logic Synthesis Flow Orchestrator")
    parser.add_argument("-i", "--input", required=True, help="Input annotated Verilog file")
    parser.add_argument("-o", "--output-dir", default="build/axc_output", help="Output directory")
    parser.add_argument("-t", "--tech", default="NanGate15nm", help="Standard cell tech library")
    parser.add_argument("--target", default="fpga", choices=["asic", "fpga"], help="Target architecture: fpga (Xilinx 7-Series) or asic")
    parser.add_argument("--fpga-family", default="xc7", help="Target FPGA family (default: xc7)")
    parser.add_argument("--fpga-device", default="xc7a35t", help="Target FPGA reference device (default: xc7a35t)")
    parser.add_argument("--allow-dsp", action="store_true", help="Allow DSP slice inference on FPGA (default maps arithmetic to soft-logic LUTs/carry)")
    parser.add_argument("--clk", "--clk-port", dest="clk", default="clk", help="Clock port name")
    parser.add_argument("--period", "--clk-period", dest="period", type=float, default=10.0, help="Clock period in ns")
    parser.add_argument("-s", "--samples", type=int, default=300, help="Simulation samples")
    parser.add_argument("-n", "--verify-cycles", type=int, default=300, help="Miter verification clock cycles")
    parser.add_argument("--decompose", action="store_true", help="Decompose coarse-grain arithmetic cells into bit-level primitives")
    parser.add_argument("-a", "--method", default="auto", choices=["auto", "pyals", "als", "inouts", "probprun", "decisiontree", "dt", "decision_tree", "carry_prune", "lut_prune", "loa", "bam", "ppt"], help="Logic approximation backend algorithm")
    parser.add_argument("-b", "--bound", type=float, default=None, help="Override target error bound")
    parser.add_argument("--no-trace", action="store_true", help="Disable realistic pipeline trace extraction")
    parser.add_argument("--stimulus-file", default=None, help="Top-level primary input trace stimulus file")
    parser.add_argument("--no-formal", action="store_true", help="Disable Yosys SAT formal verification")
    parser.add_argument("--formal-timeout", type=int, default=60, help="Timeout in seconds for SAT solver")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    flow = AxcFlow(
        input_verilog=args.input,
        output_dir=args.output_dir,
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
        axc_method=args.method,
        override_bound=args.bound,
        target=args.target,
        fpga_family=args.fpga_family,
        fpga_device=args.fpga_device,
        fpga_nodsp=not args.allow_dsp,
    )
    result = flow.run()
    print(json.dumps(result, indent=2))
    if result.get("flow_status") != "SUCCESS":
        sys.exit(1)


if __name__ == "__main__":
    main()
