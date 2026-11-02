"""Vivado Batch Mode Synthesis and Area Optimization Flow for AxFPGA (Plan 1.3).

Provides automated generation and execution of Xilinx Vivado batch TCL scripts
for Zynq-7000 (Zybo xc7z010, PYNQ xc7z020) and Artix-7 targets, enforcing:
1. Cross-boundary constant propagation: -flatten_hierarchy full -directive AreaOptimized_high
2. Physical Area Exploration: opt_design -directive ExploreArea
3. Physical Slice LUT, LUT6_2, and CARRY4 resource reporting.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("axfpga.vivado")

SUPPORTED_FPGA_PARTS: Dict[str, str] = {
    "zybo": "xc7z010clg400-1",
    "zybo-z7": "xc7z010clg400-1",
    "xc7z010": "xc7z010clg400-1",
    "xc7z010clg400-1": "xc7z010clg400-1",
    "pynq": "xc7z020clg400-1",
    "pynq-z1": "xc7z020clg400-1",
    "pynq-z2": "xc7z020clg400-1",
    "xc7z020": "xc7z020clg400-1",
    "xc7z020clg400-1": "xc7z020clg400-1",
    "artix7": "xc7a35ticsg324-1L",
    "xc7a35t": "xc7a35ticsg324-1L",
    "xc7a35ticsg324-1L": "xc7a35ticsg324-1L",
    "basys3": "xc7a35tcpg236-1",
}


def resolve_part(target_part_or_board: str) -> str:
    """Resolve board alias (e.g. 'zybo', 'pynq') to physical Xilinx 7-Series part number."""
    key = target_part_or_board.lower().strip()
    return SUPPORTED_FPGA_PARTS.get(key, target_part_or_board)


class VivadoBatchRunner:
    """Manages Vivado batch mode synthesis for Xilinx 7-Series."""

    def __init__(
        self,
        part: str = "xc7z010clg400-1",
        vivado_bin: Optional[str] = None,
    ):
        self.part = resolve_part(part)
        self.vivado_bin = vivado_bin or shutil.which("vivado")

    def generate_tcl(
        self,
        top_module: str,
        verilog_files: List[str | Path],
        out_tcl_path: str | Path,
        sdc_file: Optional[str | Path] = None,
        directive: str = "AreaOptimized_high",
        opt_directive: str = "ExploreArea",
    ) -> Path:
        """Generate high-area-optimization batch TCL script as specified in Plan 1.3."""
        tcl_path = Path(out_tcl_path).resolve()
        tcl_path.parent.mkdir(parents=True, exist_ok=True)

        vlog_commands = []
        for vf in verilog_files:
            vlog_commands.append(f'read_verilog -sv "{Path(vf).resolve()}"')
        vlog_block = "\n".join(vlog_commands)

        sdc_block = ""
        if sdc_file and Path(sdc_file).exists():
            sdc_block = f'read_xdc "{Path(sdc_file).resolve()}"'

        tcl_content = f"""# AxFPGA Plan 1.3 Vivado Sign-Off Batch Script
# Target Part: {self.part} | Top: {top_module}
set_param general.maxThreads 4

# Read design sources
{vlog_block}
{sdc_block}

# Phase 1.3: Enforce cross-boundary constant propagation and aggressive area optimization
synth_design -top {top_module} -part {self.part} -flatten_hierarchy full -directive {directive}

# Post-synthesis logic and physical packing optimization
opt_design -directive {opt_directive}

# Generate sign-off utilization and timing reports
report_utilization -file utilization.rpt
report_utilization -hierarchical -file utilization_hierarchical.rpt
report_timing_summary -file timing_summary.rpt

# Write sign-off post-synthesis gate-level netlist
write_verilog -force -mode design post_synth_netlist.v
write_edif -force post_synth_netlist.edif

puts "AxFPGA Vivado Batch Run Completed Successfully for {top_module} on {self.part}."
"""
        tcl_path.write_text(tcl_content, encoding="utf-8")
        logger.info("Generated Vivado sign-off TCL script: %s", tcl_path)
        return tcl_path

    def run(
        self,
        top_module: str,
        verilog_files: List[str | Path],
        work_dir: str | Path,
        sdc_file: Optional[str | Path] = None,
        timeout: int = 1200,
    ) -> Dict[str, Any]:
        """Execute Vivado batch mode synthesis if binary is available, returning utilization metrics."""
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        tcl_file = work_dir / "run_vivado_synth.tcl"
        self.generate_tcl(
            top_module=top_module,
            verilog_files=verilog_files,
            out_tcl_path=tcl_file,
            sdc_file=sdc_file,
        )

        if not self.vivado_bin:
            logger.info("Vivado binary not in PATH. Generated TCL script at %s for manual/batch execution.", tcl_file)
            return {
                "status": "TCL_GENERATED",
                "part": self.part,
                "tcl_script": str(tcl_file),
                "work_dir": str(work_dir),
            }

        cmd = [self.vivado_bin, "-mode", "batch", "-source", str(tcl_file)]
        log_file = work_dir / "vivado_synth.log"
        logger.info("Running Vivado batch synthesis on %s (%s)...", top_module, self.part)

        with open(log_file, "w", encoding="utf-8") as lf:
            proc = subprocess.run(
                cmd,
                cwd=work_dir,
                stdout=lf,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                text=True,
            )

        rpt_file = work_dir / "utilization.rpt"
        if proc.returncode == 0 and rpt_file.exists():
            metrics = self.parse_utilization_report(rpt_file)
            metrics["status"] = "SUCCESS"
            metrics["part"] = self.part
            metrics["tcl_script"] = str(tcl_file)
            metrics["log_file"] = str(log_file)
            return metrics

        logger.warning("Vivado batch execution finished with code %d. See %s", proc.returncode, log_file)
        return {
            "status": "FAILED",
            "return_code": proc.returncode,
            "part": self.part,
            "tcl_script": str(tcl_file),
            "log_file": str(log_file),
        }

    @staticmethod
    def parse_utilization_report(rpt_path: str | Path) -> Dict[str, Any]:
        """Parse Vivado utilization.rpt for Slice LUTs, LUT6, LUT5, CARRY4, and DSP48E1."""
        content = Path(rpt_path).read_text(encoding="utf-8", errors="ignore")
        res: Dict[str, Any] = {
            "slice_luts": 0,
            "lut_as_logic": 0,
            "carry4": 0,
            "dsp48e1": 0,
            "slice_registers": 0,
            "lut6_2_pairs": 0,
        }

        # Regex extractors for standard Vivado tabular report format
        lut_m = re.search(r"\|\s*Slice LUTs\s*\|\s*(\d+)", content)
        if lut_m:
            res["slice_luts"] = int(lut_m.group(1))

        logic_m = re.search(r"\|\s*LUT as Logic\s*\|\s*(\d+)", content)
        if logic_m:
            res["lut_as_logic"] = int(logic_m.group(1))

        c4_m = re.search(r"\|\s*CARRY4\s*\|\s*(\d+)", content)
        if c4_m:
            res["carry4"] = int(c4_m.group(1))

        dsp_m = re.search(r"\|\s*DSPs\s*\|\s*(\d+)", content) or re.search(r"\|\s*DSP48E1\s*\|\s*(\d+)", content)
        if dsp_m:
            res["dsp48e1"] = int(dsp_m.group(1))

        ff_m = re.search(r"\|\s*Slice Registers\s*\|\s*(\d+)", content)
        if ff_m:
            res["slice_registers"] = int(ff_m.group(1))

        # LUT6_2 / fully used LUT-FF pairs
        lut6_2_m = re.search(r"\|\s*LUT6_2\s*\|\s*(\d+)", content)
        if lut6_2_m:
            res["lut6_2_pairs"] = int(lut6_2_m.group(1))

        # Estimated Logic Cells: LUT + 2*CARRY4 + 100*DSP
        luts = res["slice_luts"] or res["lut_as_logic"]
        res["estimated_lcs"] = luts + (2 * res["carry4"]) + (100 * res["dsp48e1"])
        return res
