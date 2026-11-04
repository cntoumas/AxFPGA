"""Top-level flow and CLI for attribute-driven approximate computing on FPGA.

Reads Verilog sources annotated with (* axc_relax *) / (* axc_precise *) / (* axc_bound *), runs the pipeline
(axfpga/pipeline.py: exact synthesis, design-level PPG pruning, pyALS Candidate Tournament with fallback,
simulation and formal checks, power) and adds the SDC timing constraints and a Vivado script.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from axfpga.cone_extractor import ConeExtractor
from axfpga.estimator import FpgaEstimator
from axfpga.pipeline import DesignSpec, GenericEvaluator, _top_ports, run_design
from axfpga.sdc_generator import SdcGenerator

logger = logging.getLogger("axc_flow")


class AxcFlow:
    """Full-flow execution manager."""

    def __init__(
        self,
        input_verilog: str | Path | Sequence[str | Path],
        output_dir: str | Path,
        clk_port: str = "clk",
        clk_period_ns: float = 10.0,
        sim_samples: int = 1000,
        verify_cycles: int = 500,
        decompose_operators: bool = False,
        use_realistic_traces: bool = True,
        stimulus_file: Optional[str | Path] = None,
        enable_formal: bool = True,
        formal_timeout: int = 60,
        axc_method: str = "pyals",
        override_bound: Optional[float] = None,
        fpga_family: str = "xc7",
        fpga_device: str = "xc7a35t",
        fpga_nodsp: bool = False,
        top: Optional[str] = None,
        dsp: Optional[bool] = None,
    ):
        files = [input_verilog] if isinstance(input_verilog, (str, Path)) else list(input_verilog)
        self.input_files: List[Path] = [Path(f).resolve() for f in files]
        self.input_verilog = self.input_files[0]
        self.output_dir = Path(output_dir).resolve()
        self.top = top
        self.clk_port = clk_port
        self.clk_period_ns = clk_period_ns
        self.sim_samples = sim_samples
        self.override_bound = float(override_bound) if override_bound is not None else None
        self.fpga_family = fpga_family.lower()
        self.fpga_device = fpga_device.lower()
        self.fpga_nodsp = (not dsp) if dsp is not None else fpga_nodsp
        self.axc_method = axc_method
        # Accepted for API compatibility (sweep/budgeter); the pipeline fixes these: 1000-cycle miter, 120 s SAT
        # timeout, formal checks always on, random stimulus.
        ignored = []
        if not enable_formal:
            ignored.append("enable_formal=False")
        if stimulus_file:
            ignored.append("stimulus_file")
        if ignored:
            logger.warning("Ignored by the pipeline: %s", ", ".join(ignored))

        self.output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_signed(files: Sequence[Path], wire: str) -> bool:
        for f in files:
            if re.search(rf"\bsigned\b[^;,)]*\b{re.escape(wire)}\b", f.read_text(errors="ignore")):
                return True
        return False

    def run(self) -> Dict[str, Any]:
        """Execute the end-to-end flow."""
        start = time.time()
        logger.info("=== STARTING AXC EDA FLOW ===")
        logger.info("Input Verilog: %s", ", ".join(map(str, self.input_files)))
        logger.info("Output Directory: %s | target %s/%s | DSP %s", self.output_dir, self.fpga_family,
                    self.fpga_device, "off" if self.fpga_nodsp else "on")

        # Discover top, relaxed output and budget from the attributes in the sources.
        scan_dir = self.output_dir / "scan"
        scan_dir.mkdir(parents=True, exist_ok=True)
        scan = ConeExtractor(self.input_files, top_module=self.top).extract_safe_cone(
            submod_tag="axc_cone_0", out_submod_path=scan_dir / "cone.v",
            out_top_path=scan_dir / "shell.v", out_meta_path=scan_dir / "meta.json")
        top = scan.get("top_module") or self.top or "unknown"
        relaxed = scan.get("relaxed_signals", [])
        if scan.get("status") == "NO_RELAX_SIGNALS" or not relaxed:
            summary = {"flow_status": "STOPPED", "reason": scan.get("status", "NO_RELAX_SIGNALS"), "top_module": top,
                       "elapsed_seconds": round(time.time() - start, 2), "details": scan}
            (self.output_dir / "flow_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return summary
        shutil.rmtree(scan_dir, ignore_errors=True)

        bounds = [float(s["bound"]) for s in relaxed if s.get("bound") is not None]
        if self.override_bound is not None:
            bound = self.override_bound
        elif len(bounds) == len(relaxed):
            bound = min(bounds)          # most restrictive of several relaxed signals
        else:
            missing = [s.get("wire") for s in relaxed if s.get("bound") is None]
            raise ValueError(f"(* axc_relax *) signal(s) {missing} have no axc_bound; annotate them or pass --bound")
        wire = relaxed[0]["wire"]
        if len(relaxed) > 1:
            logger.warning("%d relaxed signals; error statistics are measured on %s", len(relaxed), wire)
        metric = str(relaxed[0].get("metric") or "WCE").upper()

        ports = _top_ports(self.input_files, top)
        width = dict(ports["outputs"]).get(wire)
        if width is None:
            summary = {"flow_status": "STOPPED", "top_module": top, "elapsed_seconds": round(time.time() - start, 2),
                       "reason": f"relaxed signal {wire!r} is not an output port of {top}; error statistics need a top output"}
            (self.output_dir / "flow_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return summary
        evaluator = GenericEvaluator(ports["inputs"], width, n_vectors=max(self.sim_samples, 64),
                                     signed=self._is_signed(self.input_files, wire))
        spec = DesignSpec(id=top, top=top, files=self.input_files, relax_wire=wire, bound=bound, metric=metric,
                          annotated=True, med_limit=None, mred_limit=None)
        work = self.output_dir / "work"
        est = FpgaEstimator(family=self.fpga_family, device=self.fpga_device, nodsp=self.fpga_nodsp)
        result = run_design(spec, est, work_dir=work, evaluator=evaluator, tournament_estimator=est)
        result["device"] = self.fpga_device
        # Schema-valid record list (docs/schema/result.schema.json is an array of results)
        (self.output_dir / "result.json").write_text(json.dumps([result], indent=2, default=str), encoding="utf-8")

        # SDC timing relaxation from the attribute metadata
        sdc = self.output_dir / "final_approx_top.sdc"
        meta_path = work / "cone_meta.json"
        if meta_path.exists():
            SdcGenerator(clk_port=self.clk_port, clk_period_ns=self.clk_period_ns, relaxation_factor=1.5,
                         constraint_mode="max_delay").generate_sdc(metadata=json.loads(meta_path.read_text()),
                                                                   out_sdc_path=sdc)
        final_top = work / "final_approx_top.v"
        files = {"final_approx_top": str(final_top), "exact_mapped": str(work / "exact_mapped.v"),
                 "approx_mapped": str(work / "approx_mapped.v"), "sdc_constraints": str(sdc) if sdc.exists() else None,
                 "metadata": str(meta_path)}
        try:
            from axfpga.vivado import VivadoBatchRunner
            tcl = self.output_dir / "run_vivado_synth.tcl"
            VivadoBatchRunner(part=self.fpga_device or "xc7z010clg400-1").generate_tcl(
                top_module=top, verilog_files=[final_top], out_tcl_path=tcl, sdc_file=sdc if sdc.exists() else None)
            files["vivado_tcl"] = str(tcl)
        except Exception as exc:
            logger.warning("Vivado script not generated: %s", exc)

        formal = result["formal"]
        summary: Dict[str, Any] = {
            "flow_status": "NO_APPROXIMATION" if result["fell_back"] else "SUCCESS",
            "target": "fpga",
            "dsp": not self.fpga_nodsp,
            "elapsed_seconds": round(time.time() - start, 2),
            "top_module": top,
            "formal_control_status": formal["control_equivalence"],
            "formal_wce_status": formal["wce_bound"],
            "approximation": {"metric": metric, "bound": bound, "relaxed_signals": relaxed,
                              "measured_error": result["errors"], "champion_method": result["method"],
                              "fell_back": result["fell_back"], "fallback_reason": result["fallback_reason"]},
            "fpga": {"family": self.fpga_family, "device": self.fpga_device, "exact": result["exact"],
                     "approx": result["approx"], "savings_pct": result["savings_pct"]},
            "result": result,
            "generated_files": files,
        }
        (self.output_dir / "flow_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        logger.info("=== FLOW FINISHED IN %.2f SECONDS ===", summary["elapsed_seconds"])
        return summary


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="AxFPGA: attribute-driven approximate logic synthesis for FPGAs")
    parser.add_argument("-i", "--input", nargs="+", required=True, metavar="FILE", help="Annotated Verilog file(s)")
    parser.add_argument("--top", default=None, help="Top module (default: auto-detected)")
    parser.add_argument("-o", "--out-dir", "--output-dir", dest="out_dir", default="build/axc_output", help="Output directory")
    parser.add_argument("--dsp", choices=["on", "off"], default="on", help="Allow DSP slice inference (default: on)")
    parser.add_argument("--nodsp", action="store_true", help="Alias for --dsp off")
    parser.add_argument("--fpga-family", default="xc7", help="Target FPGA family (default: xc7)")
    parser.add_argument("--fpga-device", default="xc7a35t", help="Target FPGA reference device (default: xc7a35t)")
    parser.add_argument("--clk", "--clk-port", dest="clk", default="clk", help="Clock port name for the SDC")
    parser.add_argument("--period", "--clk-period", dest="period", type=float, default=10.0, help="Clock period in ns")
    parser.add_argument("-s", "--samples", type=int, default=1000, help="Random stimulus cycles for error measurement")
    parser.add_argument("-b", "--bound", type=float, default=None, help="Error bound; overrides axc_bound")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    # Deprecated, accepted and ignored
    parser.add_argument("-n", "--verify-cycles", type=int, default=300, help=argparse.SUPPRESS)
    parser.add_argument("--decompose", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-a", "--method", default="pyals", choices=["pyals", "als", "auto"], help=argparse.SUPPRESS)
    parser.add_argument("--no-trace", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stimulus-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--no-formal", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--formal-timeout", type=int, default=60, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    flow = AxcFlow(
        input_verilog=args.input,
        output_dir=args.out_dir,
        top=args.top,
        clk_port=args.clk,
        clk_period_ns=args.period,
        sim_samples=args.samples,
        enable_formal=not args.no_formal,
        stimulus_file=args.stimulus_file,
        override_bound=args.bound,
        fpga_family=args.fpga_family,
        fpga_device=args.fpga_device,
        dsp=(args.dsp == "on") and not args.nodsp,
    )
    # Yosys (in-process) writes its log to fd 1 through C stdio: point fd 1 at stderr while the flow runs, flush the
    # C buffers before restoring it, then print the JSON result.
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    try:
        result = flow.run()
    finally:
        try:
            ctypes.CDLL(None).fflush(None)
        except OSError:
            pass
        os.dup2(saved, 1)
        os.close(saved)
    print(json.dumps(result, indent=2, default=str))
    if result.get("flow_status") not in ("SUCCESS", "NO_APPROXIMATION"):
        sys.exit(1)


if __name__ == "__main__":
    main()
