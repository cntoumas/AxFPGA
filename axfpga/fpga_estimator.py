"""FPGA Resource and Utilization Estimator for AxSYS.

Synthesizes Verilog netlists to target FPGA architectures (primarily Xilinx 7-Series / Artix-7)
using Yosys `synth_xilinx` and extracts physical resource utilization metrics:
- 6-input LUTs (LUT1..LUT6 total and breakdown)
- Dedicated fast-carry chains (CARRY4)
- Flip-Flop registers (FDRE, FDCE, FDSE, FDPE)
- Digital Signal Processing blocks (DSP48E1)
- Estimated Logic Cells (LCs)
- Device footprint percentage on target FPGA (e.g., Artix-7 XC7A35T or XC7A100T)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("fpga_estimator")

# Standard Reference FPGA Device Capacities
FPGA_DEVICE_CAPACITIES: Dict[str, Dict[str, int]] = {
    "xc7a35t": {
        "luts": 20800,
        "ffs": 41600,
        "carry4": 5200,
        "dsps": 90,
        "bram36": 50,
        "slices": 8150,
    },
    "xc7a100t": {
        "luts": 63400,
        "ffs": 126800,
        "carry4": 15850,
        "dsps": 240,
        "bram36": 135,
        "slices": 15850,
    },
    "xc7z020": {
        "luts": 53200,
        "ffs": 106400,
        "carry4": 13300,
        "dsps": 220,
        "bram36": 140,
        "slices": 13300,
    },
}


class FpgaEstimator:
    """Evaluates FPGA resource utilization and computes savings for approximate circuits."""

    def __init__(
        self,
        family: str = "xc7",
        device: str = "xc7a35t",
        nodsp: bool = False,
    ):
        self.family = family.lower()
        self.device = device.lower()
        self.nodsp = nodsp
        self.device_capacity = FPGA_DEVICE_CAPACITIES.get(
            self.device, FPGA_DEVICE_CAPACITIES["xc7a35t"]
        )

    def evaluate_netlist(
        self,
        netlist_path: str | Path,
        top_module: str,
        out_mapped_vlog: Optional[str | Path] = None,
        work_dir: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        """Synthesize a Verilog netlist to target FPGA primitives and extract resource counts."""
        netlist = Path(netlist_path).resolve()
        if not netlist.exists():
            raise FileNotFoundError(f"Netlist file not found: {netlist}")

        t0 = time.time()
        dsp_flag = "-nodsp" if self.nodsp else ""
        out_vlog_cmd = f"write_verilog {out_mapped_vlog};" if out_mapped_vlog else ""

        # If netlist references gate primitives (e.g. from NanGate15nm) without embedding definitions, read them first
        extra_reads = ""
        try:
            content = netlist.read_text(encoding="utf-8", errors="ignore")
            if ("module AND2_X1" not in content) and ("_X" in content or "MUX2" in content or "FA_X1" in content):
                nan_path = Path(__file__).resolve().parent.parent / "third_party" / "AxLS" / "templates" / "NanGate15nm.v"
                if nan_path.exists():
                    extra_reads = f"read_verilog -sv {nan_path}; "
        except Exception:
            pass

        yosys_script = (
            f"{extra_reads}"
            f"read_verilog -sv {netlist}; "
            f"synth_xilinx -family {self.family} -flatten -noiopad {dsp_flag} -top {top_module}; "
            f"{out_vlog_cmd} "
            f"ltp; "
            f"stat -json"
        )

        cmd = ["yosys", "-p", yosys_script]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(work_dir) if work_dir else None,
        )

        if proc.returncode != 0:
            logger.error("Yosys synth_xilinx failed: %s", proc.stderr)
            raise RuntimeError(f"Yosys synth_xilinx failed with code {proc.returncode}: {proc.stderr}")

        # Parse JSON stat output
        stat_data = self._extract_json_stat(proc.stdout)
        elapsed = time.time() - t0

        cells_by_type: Dict[str, int] = {}
        if stat_data and "design" in stat_data and "num_cells_by_type" in stat_data["design"]:
            cells_by_type = stat_data["design"]["num_cells_by_type"]
        elif stat_data and "modules" in stat_data:
            # Fallback to module key
            mod_key = f"\\{top_module}"
            if mod_key in stat_data["modules"]:
                cells_by_type = stat_data["modules"][mod_key].get("num_cells_by_type", {})

        # Compute FPGA primitives
        luts_by_type = {k: v for k, v in cells_by_type.items() if k.startswith("LUT")}
        total_luts = sum(luts_by_type.values())
        carry4_count = cells_by_type.get("CARRY4", 0)
        ff_count = sum(v for k, v in cells_by_type.items() if k.startswith("FD"))
        dsp_count = cells_by_type.get("DSP48E1", 0)
        muxf_count = sum(v for k, v in cells_by_type.items() if k.startswith("MUXF"))
        ramb36_count = cells_by_type.get("RAMB36E1", 0)
        ramb18_count = cells_by_type.get("RAMB18E1", 0)
        bram_count = ramb36_count + ramb18_count

        # Estimated Logic Cells (Xilinx standard formula: LUTs + 2 * CARRY4)
        m_lc = re.search(r"Estimated number of LCs:\s*(\d+)", proc.stdout)
        stat_lcs = int(m_lc.group(1)) if m_lc else 0
        est_lcs = max(total_luts + 2 * carry4_count, stat_lcs)

        lut_cap = self.device_capacity["luts"]
        ff_cap = self.device_capacity["ffs"]

        # Longest topological path (timing)
        m_ltp = re.search(r"Longest topological path in [^\n]+\(length=(\d+)\)", proc.stdout)
        logic_levels = int(m_ltp.group(1)) if m_ltp else 1
        crit_delay_ns = round(max(0.2, logic_levels * 0.474), 3)
        fmax_mhz = round(1000.0 / crit_delay_ns, 1) if crit_delay_ns > 0 else 0.0

        # Power estimation on target FPGA (XC7A35T)
        static_pwr_mw = 68.0
        dyn_pwr_mw = round(total_luts * 0.25 + carry4_count * 0.15 + ff_count * 0.08 + dsp_count * 4.5 + bram_count * 8.0, 2)
        total_pwr_mw = round(static_pwr_mw + dyn_pwr_mw, 2)

        return {
            "top_module": top_module,
            "family": self.family,
            "device": self.device,
            "total_luts": total_luts,
            "luts_by_type": luts_by_type,
            "carry4": carry4_count,
            "ffs": ff_count,
            "dsps": dsp_count,
            "muxf": muxf_count,
            "brams": bram_count,
            "ramb36": ramb36_count,
            "ramb18": ramb18_count,
            "estimated_lcs": est_lcs,
            "logic_levels": logic_levels,
            "critical_path_delay_ns": crit_delay_ns,
            "fmax_mhz": fmax_mhz,
            "static_power_mw": static_pwr_mw,
            "dynamic_power_mw": dyn_pwr_mw,
            "total_power_mw": total_pwr_mw,
            "device_capacity": self.device_capacity,
            "lut_utilization_pct": round((total_luts / lut_cap) * 100.0, 3) if lut_cap else 0.0,
            "ff_utilization_pct": round((ff_count / ff_cap) * 100.0, 3) if ff_cap else 0.0,
            "cells_by_type": cells_by_type,
            "synth_time_sec": round(elapsed, 2),
        }

    def compare_exact_vs_approx(
        self,
        exact_netlist: str | Path,
        approx_netlist: str | Path,
        top_module: str,
        work_dir: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        """Evaluate and compare exact baseline vs approximate netlist on FPGA."""
        exact_res = self.evaluate_netlist(
            exact_netlist,
            top_module=top_module,
            work_dir=work_dir,
        )
        approx_res = self.evaluate_netlist(
            approx_netlist,
            top_module=top_module,
            work_dir=work_dir,
        )

        base_luts = exact_res["total_luts"]
        app_luts = approx_res["total_luts"]
        luts_saved = base_luts - app_luts
        lut_red_pct = ((luts_saved) / base_luts * 100.0) if base_luts > 0 else 0.0

        base_carry = exact_res["carry4"]
        app_carry = approx_res["carry4"]
        carry_saved = base_carry - app_carry
        carry_red_pct = ((carry_saved) / base_carry * 100.0) if base_carry > 0 else 0.0

        base_lcs = exact_res["estimated_lcs"]
        app_lcs = approx_res["estimated_lcs"]
        lcs_saved = base_lcs - app_lcs
        lc_red_pct = ((lcs_saved) / base_lcs * 100.0) if base_lcs > 0 else 0.0

        base_delay = exact_res["critical_path_delay_ns"]
        app_delay = approx_res["critical_path_delay_ns"]
        delay_saved_ns = round(base_delay - app_delay, 3)
        delay_saved_pct = round(((delay_saved_ns) / base_delay * 100.0), 2) if base_delay > 0 else 0.0

        base_dyn_pwr = exact_res["dynamic_power_mw"]
        app_dyn_pwr = approx_res["dynamic_power_mw"]
        dyn_pwr_saved = round(base_dyn_pwr - app_dyn_pwr, 2)
        dyn_pwr_saved_pct = round(((dyn_pwr_saved) / base_dyn_pwr * 100.0), 2) if base_dyn_pwr > 0 else 0.0

        base_tot_pwr = exact_res["total_power_mw"]
        app_tot_pwr = approx_res["total_power_mw"]
        tot_pwr_saved = round(base_tot_pwr - app_tot_pwr, 2)
        tot_pwr_saved_pct = round(((tot_pwr_saved) / base_tot_pwr * 100.0), 2) if base_tot_pwr > 0 else 0.0

        return {
            "top_module": top_module,
            "family": self.family,
            "device": self.device,
            "exact": exact_res,
            "approx": approx_res,
            "luts_saved": luts_saved,
            "lut_reduction_pct": round(lut_red_pct, 2),
            "carry_saved": carry_saved,
            "carry_reduction_pct": round(carry_red_pct, 2),
            "lcs_saved": lcs_saved,
            "lc_reduction_pct": round(lc_red_pct, 2),
            "delay_saved_ns": delay_saved_ns,
            "delay_saved_pct": delay_saved_pct,
            "dynamic_power_saved_mw": dyn_pwr_saved,
            "dynamic_power_saved_pct": dyn_pwr_saved_pct,
            "total_power_saved_mw": tot_pwr_saved,
            "total_power_saved_pct": tot_pwr_saved_pct,
        }

    @staticmethod
    def _extract_json_stat(stdout: str) -> Optional[Dict[str, Any]]:
        """Extract the JSON block emitted by Yosys stat -json."""
        brace_start = stdout.find("{\n   \"creator\":")
        if brace_start == -1:
            brace_start = stdout.find("{\n  \"creator\":")
        if brace_start == -1:
            # Look for general opening brace containing "creator"
            idx = stdout.find('"creator":')
            if idx != -1:
                brace_start = stdout.rfind("{", 0, idx)

        if brace_start != -1:
            try:
                # Find the matching closing brace
                brace_end = stdout.rfind("}")
                if brace_end != -1 and brace_end > brace_start:
                    json_str = stdout[brace_start : brace_end + 1]
                    return json.loads(json_str)
            except Exception as e:
                logger.debug("Failed to parse Yosys stat JSON: %s", e)
        return None
