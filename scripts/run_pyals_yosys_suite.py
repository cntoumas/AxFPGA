#!/usr/bin/env python3
"""AxBench evaluation of AxFPGA on the Zybo Z7-10 (xc7z010clg400-1), Yosys only.

Per benchmark:
1. Yosys synth_xilinx of the exact design (DSP inference allowed).
2. (* axc_relax *)/(* axc_precise *) annotation and safe Cone extraction.
3. pyALS Candidate Tournament on the Cone; reintegration into the top.
4. Yosys synth_xilinx of the approximate design.
5. Random-stimulus miter (iverilog) + Yosys SAT formal control/WCE proofs;
   fallback to the exact design if the top-level WCE exceeds the bound.
6. Application-level metric (scripts/axbench_apps.py) on exact vs approximate RTL.
7. Activity-based dynamic power (axfpga/activity_power.py) of both mapped netlists.

Outputs (in --out-dir, default reports/): results.json, results.csv, report.md.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import math
import multiprocessing as mp
import os
import pickle
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# AxFPGA native components
from pyosys import libyosys as ys
from axfpga.cone_extractor import ConeExtractor
from axfpga.pyals_bridge import PyAlsBridge
from axfpga.ppg_pruner import PpgPruner
from axfpga.reintegrator import Reintegrator
from axfpga.estimator import FpgaEstimator
from axfpga.tournament import Tournament
from axfpga.verifier import FormalControlVerifier, FormalWceVerifier, MiterVerifier
from axfpga.activity_power import estimate_dynamic_power, run_vector_sim
from axfpga.parallel import Cancelled, depth_search, first_accepted, yosys_serialized
from axfpga.config import axbench_root, yosys_cmd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import axbench_apps  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pyals_yosys_suite")

AXBENCH_ROOT = axbench_root() / "circuits"          # AXFPGA_AXBENCH_ROOT overrides (see axfpga/config.py)
AXFPGA_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = AXFPGA_ROOT / "build" / "pyals_yosys_run"
REPORTS_DIR = AXFPGA_ROOT / "reports"
FPGA_PART = "xc7z010clg400-1"
FPGA_DEVICE = "xc7z010"
F_CLK_MHZ = 125.0
FORMAL_TIMEOUT_S = 120
SIM_TIMEOUT_S = 3600           # miter compile/run timeout (large designs, e.g. sobel_npu at 42k LUTs)
MAX_ATTEMPTS = 8               # candidates tried per benchmark before keeping the exact design
MED_FRACTION_OF_BOUND = 0.5    # MED limit = WCE bound / 2
MRED_LIMIT = 0.01              # MRED limit = 1 %
MAX_PPG_EVALS = 7              # binary-search steps over the PPG anti-diagonal depth
CONE_VARIANTS = ("raw", "remapped")  # reintegration variants tried per candidate, in order (--cone-variants)
INNER_JOBS = 3                 # concurrent candidate evaluations inside one benchmark (--inner-jobs)

# Scheduling hints for --jobs > 1. Relative run time orders the queue (longest first);
# EST_GB is the peak resident memory of one concurrent synthesis (tune from build/*/stats.json);
# BASE_GB is the benchmark process itself (python + pyosys + numpy).
EST_COST = {"sobel_npu": 100, "neural_network": 40, "kinematics_inv": 40, "multiplier_32b": 25, "kinematics_fwd": 15}
EST_GB = {"sobel_npu": 10.0, "kinematics_inv": 3.0, "multiplier_32b": 1.0, "kinematics_fwd": 2.0, "neural_network": 1.0}  # sobel_npu: 6.7 GB yosys, 9.3 GB iverilog miter
DEFAULT_GB = 0.4
BASE_GB = 1.4
RESERVE_GB = 2.0               # never plan to use the last RESERVE_GB of available memory
HOLD_S = 120                   # cooldown after a watchdog kill
KILL_BELOW_GB = 1.2            # watchdog: below this much available memory the newest benchmark is stopped and requeued


def annotate_verilog(
    input_files: List[Path],
    output_dir: Path,
    top_module: str,
    relax_wire: str,
    precise_signals: List[str],
    bound: float,
    metric: str = "wce",
) -> List[Path]:
    """Insert IEEE Verilog attributes (* axc_relax *) and (* axc_precise *) into design files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    annotated_files: List[Path] = []

    attr_relax = f'(* axc_relax, axc_metric="{metric.lower()}", axc_bound={int(bound)} *) '
    attr_precise = "(* axc_precise *) "

    for fpath in input_files:
        content = fpath.read_text(encoding="utf-8", errors="ignore")
        modified = content

        # Target the top module specifically if present in this file
        top_m = re.search(rf"\bmodule\s+{re.escape(top_module)}\b.*?\bendmodule\b", modified, re.DOTALL)
        if top_m:
            top_block = top_m.group(0)
            pat_out = rf"(?m)^([ \t]*)output\s+((?:\[[^\]]+\]\s*)?{re.escape(relax_wire)}\b)"
            if re.search(pat_out, top_block):
                new_top = re.sub(pat_out, rf"\g<1>{attr_relax}output \g<2>", top_block, count=1)
            else:
                pat_wire = rf"(?m)^([ \t]*)wire\s+((?:\[[^\]]+\]\s*)?{re.escape(relax_wire)}\b)"
                if re.search(pat_wire, top_block):
                    new_top = re.sub(pat_wire, rf"\g<1>{attr_relax}wire \g<2>", top_block, count=1)
                else:
                    pat_any = rf"\boutput\s+((?:\[[^\]]+\]\s*)?{re.escape(relax_wire)}\b)"
                    new_top = re.sub(pat_any, rf"{attr_relax}output \g<1>", top_block, count=1)

            for p_sig in precise_signals:
                pat_in = rf"(?m)^([ \t]*)input\s+((?:\[[^\]]+\]\s*)?{re.escape(p_sig)}\b)"
                if re.search(pat_in, new_top):
                    new_top = re.sub(pat_in, rf"\g<1>{attr_precise}input \g<2>", new_top, count=1)
                else:
                    pat_in_any = rf"\binput\s+((?:\[[^\]]+\]\s*)?{re.escape(p_sig)}\b)"
                    new_top = re.sub(pat_in_any, rf"{attr_precise}input \g<1>", new_top, count=1)

            modified = modified[:top_m.start()] + new_top + modified[top_m.end():]

        out_path = output_dir / fpath.name
        out_path.write_text(modified, encoding="utf-8")
        annotated_files.append(out_path)

    return annotated_files


def _raw_benchmark_suite() -> List[Dict[str, Any]]:
    """Define the 13 axbench-verilog benchmark test specifications."""
    sobel_rtl = AXBENCH_ROOT / "sobel" / "rtl"
    kin_fwd_rtl = AXBENCH_ROOT / "kinematics.fwd" / "rtl"
    kin_inv_rtl = AXBENCH_ROOT / "kinematics.inv" / "rtl"
    km_rtl = AXBENCH_ROOT / "kmeans" / "rtl"
    nn_rtl = AXBENCH_ROOT / "neural.network" / "rtl"
    snn_rtl = AXBENCH_ROOT / "sobel.nn" / "rtl"

    return [
        {
            "id": "brent_kung_16b",
            "name": "Brent-Kung 16b",
            "domain": "Prefix Adder Arithmetic",
            "top": "UBBKA_15_0_15_0",
            "files": [AXBENCH_ROOT / "brent.kung.16b" / "rtl" / "UBBKA_15_0_15_0.v"],
            "relax_wire": "S",
            "precise_signals": [],
            "bound": 15.0,
            "metric": "WCE",
            "fidelity": {"name": "Arithmetic SQNR", "threshold": ">= 30.0 dB", "pass_floor": 30.0},
        },
        {
            "id": "brent_kung_32b",
            "name": "Brent-Kung 32b",
            "domain": "Prefix Adder Arithmetic",
            "top": "UBBKA_31_0_31_0",
            "files": [AXBENCH_ROOT / "brent.kung.32b" / "rtl" / "UBBKA_31_0_31_0.v"],
            "relax_wire": "S",
            "precise_signals": [],
            "bound": 255.0,
            "metric": "WCE",
            "fidelity": {"name": "Arithmetic SQNR", "threshold": ">= 30.0 dB", "pass_floor": 30.0},
        },
        {
            "id": "kogge_stone_16b",
            "name": "Kogge-Stone 16b",
            "domain": "Fast Adder Arithmetic",
            "top": "UBKSA_15_0_15_0",
            "files": [AXBENCH_ROOT / "kogge.stone.16b" / "rtl" / "UBKSA_15_0_15_0.v"],
            "relax_wire": "S",
            "precise_signals": [],
            "bound": 15.0,
            "metric": "WCE",
            "fidelity": {"name": "Arithmetic SQNR", "threshold": ">= 30.0 dB", "pass_floor": 30.0},
        },
        {
            "id": "kogge_stone_32b",
            "name": "Kogge-Stone 32b",
            "domain": "Fast Adder Arithmetic",
            "top": "UBKSA_31_0_31_0",
            "files": [AXBENCH_ROOT / "kogge.stone.32b" / "rtl" / "UBKSA_31_0_31_0.v"],
            "relax_wire": "S",
            "precise_signals": [],
            "bound": 255.0,
            "metric": "WCE",
            "fidelity": {"name": "Arithmetic SQNR", "threshold": ">= 30.0 dB", "pass_floor": 30.0},
        },
        {
            "id": "multiplier_16b",
            "name": "Multiplier 16b",
            "domain": "Multiplier Arithmetic",
            "top": "Multiplier_15_0_1000",
            "files": [AXBENCH_ROOT / "multiplier.16b" / "rtl" / "Multiplier_15_0_1000.v"],
            "relax_wire": "P",
            "precise_signals": ["clk", "rst"],
            "bound": 255.0,
            "metric": "WCE",
            "fidelity": {"name": "Product SQNR", "threshold": ">= 25.0 dB", "pass_floor": 25.0},
        },
        {
            "id": "multiplier_32b",
            "name": "Multiplier 32b",
            "domain": "Multiplier Arithmetic",
            "top": "Multiplier_31_0_3000",
            "files": [AXBENCH_ROOT / "multiplier.32b" / "rtl" / "Multiplier_31_0_3000.v"],
            "relax_wire": "P",
            "precise_signals": ["clk", "rst"],
            "bound": 4096.0,
            "metric": "WCE",
            "fidelity": {"name": "Product SQNR", "threshold": ">= 25.0 dB", "pass_floor": 25.0},
        },
        {
            "id": "fir_filter",
            "name": "FIR Filter",
            "domain": "DSP Filtering",
            "top": "filterfir",
            "files": [AXBENCH_ROOT / "fir" / "rtl" / "filterfir.v"],
            "relax_wire": "dataout",
            "precise_signals": ["clk", "rst"],
            "bound": 3.0,
            "metric": "WCE",
            "fidelity": {"name": "Output SQNR / MED", "threshold": ">= 25.0 dB", "pass_floor": 25.0},
        },
        {
            "id": "sobel_filter",
            "name": "Sobel Filter",
            "domain": "Computer Vision",
            "top": "sobel",
            "files": [
                sobel_rtl / "mux_11b.v",
                sobel_rtl / "not_11b.v",
                sobel_rtl / "exoor.v",
                sobel_rtl / "fa.v",
                sobel_rtl / "mux_8b.v",
                sobel_rtl / "sobel_add_nb.v",
                sobel_rtl / "sobel.v",
            ],
            "relax_wire": "out",
            "precise_signals": ["clk"],
            "bound": 3.0,
            "metric": "WCE",
            "fidelity": {"name": "Image PSNR / RMSE", "threshold": ">= 30.0 dB", "pass_floor": 30.0},
        },
        {
            "id": "kinematics_fwd",
            "name": "Kinematics Forward",
            "domain": "Robotics Control",
            "top": "kinematics",
            "files": [
                kin_fwd_rtl / "sin_lut.v",
                kin_fwd_rtl / "cos_lut.v",
                kin_fwd_rtl / "qadd.v",
                kin_fwd_rtl / "qmult.v",
                kin_fwd_rtl / "Multiplier_31_0_3000.v",
                kin_fwd_rtl / "kinematics.v",
            ],
            "relax_wire": "x",
            "precise_signals": ["clk", "rst"],
            "bound": 15.0,
            "metric": "WCE",
            "fidelity": {"name": "End-Effector Error", "threshold": "<= 0.05 m", "pass_floor": 40.0},
        },
        {
            "id": "kinematics_inv",
            "name": "Kinematics Inverse",
            "domain": "Robotics Control",
            "top": "inv_kin",
            "files": [
                kin_inv_rtl / "sin_lut.v",
                kin_inv_rtl / "cos_lut.v",
                kin_inv_rtl / "asin_lut.v",
                kin_inv_rtl / "acos_lut.v",
                kin_inv_rtl / "qadd.v",
                kin_inv_rtl / "qmult.v",
                kin_inv_rtl / "qdiv.v",
                kin_inv_rtl / "Multiplier_31_0_3000.v",
                kin_inv_rtl / "inv_kin.v",
            ],
            "relax_wire": "theta1",
            "precise_signals": ["clock", "rst"],
            "bound": 15.0,
            "metric": "WCE",
            "fidelity": {"name": "Joint Angle RMSE", "threshold": "<= 0.05 rad", "pass_floor": 40.0},
        },
        {
            "id": "kmeans_clustering",
            "name": "K-Means Clustering",
            "domain": "Machine Learning",
            "top": "sub_8b",
            "files": [
                km_rtl / "UBKSA_8_0_8_0.v",
                km_rtl / "sub_8b.v",
            ],
            "relax_wire": "S",
            "precise_signals": [],
            "bound": 3.0,
            "metric": "WCE",
            "fidelity": {"name": "Cluster Misclassification", "threshold": "<= 2.0%", "pass_floor": 35.0},
        },
        {
            "id": "neural_network",
            "name": "Neural Network Layer",
            "domain": "Deep Learning",
            "top": "perceptron_fixed",
            "files": [
                nn_rtl / "sigmoid_lut.v",
                nn_rtl / "qadd.v",
                nn_rtl / "qmult.v",
                nn_rtl / "Multiplier_15_0_1000.v",
                nn_rtl / "perceptron_fixed.v",
            ],
            "relax_wire": "synapse_out",
            "precise_signals": ["clk", "rst"],
            "bound": 15.0,
            "metric": "WCE",
            "fidelity": {"name": "Top-1 Accuracy Drop", "threshold": "<= 1.0%", "pass_floor": 34.0},
        },
        {
            "id": "sobel_npu",
            "name": "Sobel NPU Element",
            "domain": "Neural Computer Vision",
            "top": "perceptron_sobel",
            "files": [
                snn_rtl / "Multiplier_15_0_1000.v",
                snn_rtl / "qmult.v",
                snn_rtl / "qadd.v",
                snn_rtl / "sigmoid_lut.v",
                snn_rtl / "perceptron_fixed.v",
                snn_rtl / "perceptron_sobel.v",
            ],
            "relax_wire": "synapse_out",
            "precise_signals": ["clk"],
            "bound": 7.0,
            "metric": "WCE",
            "fidelity": {"name": "Processing PSNR", "threshold": ">= 30.0 dB", "pass_floor": 30.0},
        },
    ]



# Error budgets in the relaxed output's number format (LSB units; see axbench_apps.OUTPUT_FORMAT).
# Derived from each benchmark's application tolerance rather than a fixed LSB count:
#   - adders, FIR: unchanged (already within application needs)
#   - multipliers: WCE <= 2^(W_out/2) (half-word truncation); SQNR >= 25 dB and MRED <= 1 % still apply
#   - Sobel: PSNR >= 30 dB <=> RMSE <= 8.06 -> WCE <= 16, MED <= 8; MRED not applicable (edge image ~0)
#   - kinematics (Q15): 0.05 units/rad = 1638 LSB
#   - neural network / Sobel NPU (Q9): 0.05 of unit range = 26 LSB
#   - k-means: MRED not applicable to a distance kernel (|a-b| near 0); misclassification <= 2 % governs
# MED defaults to WCE / 2, MRED to 1 %; None disables a limit.
ERROR_BUDGET: Dict[str, Dict[str, Optional[float]]] = {
    "multiplier_16b": {"wce": float(2 ** 16)},
    "multiplier_32b": {"wce": float(2 ** 32)},
    "sobel_filter": {"wce": 16.0, "med": 8.0, "mred": None},   # edge magnitudes mostly near 0: MRED meaningless, PSNR governs
    "kinematics_fwd": {"wce": 1638.0},
    "kinematics_inv": {"wce": 1638.0},
    "neural_network": {"wce": 26.0},
    "sobel_npu": {"wce": 26.0},
    "kmeans_clustering": {"mred": None},
}

# Fixes for defects in the AxBench sources: (file name, old text, new text), applied to private copies.
RTL_PATCHES: Dict[str, List[Tuple[str, str, str]]] = {
    # sobel.nn perceptron_fixed instantiates qmult without clk/rst: every multiplier register is unclocked
    # and the design synthesizes to a constant. Wire clk through; tie the active-high rst low.
    "sobel_npu": [
        ("perceptron_fixed.v", "mul_tmp[bitwidth-1:0], over[0]);", "mul_tmp[bitwidth-1:0], over[0], clk, 1'b0);"),
        ("perceptron_fixed.v", "mul_tmp[bitwidth*i+bitwidth-1 -: bitwidth],over[i]);", "mul_tmp[bitwidth*i+bitwidth-1 -: bitwidth],over[i], clk, 1'b0);"),
    ],
}


def define_benchmark_suite() -> List[Dict[str, Any]]:
    """The 13 AxBench benchmarks with their error budgets applied."""
    suite = _raw_benchmark_suite()
    for b in suite:
        budget = ERROR_BUDGET.get(b["id"], {})
        b["bound"] = budget.get("wce", b["bound"])
        b["med_limit"] = budget["med"] if "med" in budget else b["bound"] * MED_FRACTION_OF_BOUND
        b["mred_limit"] = budget["mred"] if "mred" in budget else MRED_LIMIT
    return suite


def _patched_files(bench: Dict[str, Any], bench_work: Path) -> List[Path]:
    """Apply RTL_PATCHES to private copies of the benchmark sources (originals untouched)."""
    patches = RTL_PATCHES.get(bench["id"])
    if not patches:
        return list(bench["files"])
    out_dir = bench_work / "patched_rtl"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for f in bench["files"]:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for name, old, new in patches:
            if f.name == name:
                if old not in txt:
                    raise RuntimeError(f"RTL patch for {bench['id']} does not apply to {name}: {old!r}")
                txt = txt.replace(old, new)
        dst = out_dir / f.name
        dst.write_text(txt, encoding="utf-8")
        files.append(dst)
    logger.info("Applied %d RTL patch(es) for %s", len(patches), bench["id"])
    return files


@yosys_serialized
def _top_ports(files: List[Path], top: str) -> Dict[str, Any]:
    """Top-level ports of the exact design, with clock/reset split out."""
    d = ys.Design()
    ys.run_pass(" ".join(f"read_verilog -sv {f};" for f in files) + f" hierarchy -top {top}", d)
    mod = d.top_module()
    ins = [(w.name.str().lstrip("\\"), w.width) for w in mod.wires_.values() if w.port_input]
    outs = [(w.name.str().lstrip("\\"), w.width) for w in mod.wires_.values() if w.port_output]
    clk = next((n for n, _ in ins if n.lower() in ("clk", "clock", "clk_i")), None)
    rst = next((n for n, _ in ins if n.lower() in ("rst", "reset", "rst_i", "rst_n", "reset_n")), None)
    data_ins = [(n, w) for n, w in ins if n not in (clk, rst)]
    return {"inputs": data_ins, "outputs": outs, "clk": clk, "rst": rst,
            "rst_active_low": bool(rst and rst.lower().endswith("_n"))}


def _normalize_rtl(src: Path, top: str, out: Path) -> Path:
    """Yosys-normalized netlist (proc; flatten) with synthesis semantics.

    Some AxBench sources use blocking assignments in chained flip-flops (e.g. fir/dff),
    which race in event-driven RTL simulation; Yosys gives the hardware semantics.
    """
    lib = "read_verilog -lib +/xilinx/cells_sim.v; " if any(
        p in src.read_text(errors="ignore") for p in ("LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "CARRY4", "FDRE")) else ""
    cmd = (f"{lib}read_verilog -sv {src}; hierarchy -check -top {top}; setattr -unset keep_hierarchy; "
           f"proc; flatten; opt_clean; write_verilog -noattr {out}")
    res = subprocess.run(yosys_cmd() + ["-q", "-p", cmd], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Yosys normalization of {src.name} failed: {res.stderr[-1500:]}")
    return out


@yosys_serialized
def _reintegrate(bench: Dict[str, Any], bench_work: Path, shell_v: Path, cone_v: Path,
                 approx_cone_v: Path, final_approx_top_v: Path) -> None:
    """Stitch an approximate Cone back into the top shell, writing final_approx_top_v."""
    top_mod = bench["top"]
    if not (shell_v.exists() and cone_v.exists()):
        shutil.copy(approx_cone_v, final_approx_top_v)
        return
    if len(bench["files"]) == 1:
        Reintegrator(top_shell_path=shell_v, approx_cone_path=approx_cone_v,
                     extracted_cone_path=cone_v).reintegrate(out_final_path=final_approx_top_v, flatten=False)
        return
    # Multi-file design: keep original untouched submodules (memory/DSP behavioral primitives)
    untouched = [f.read_text(encoding="utf-8", errors="ignore") for f in bench["files"]
                 if not re.search(rf"\bmodule\s+{top_mod}\b", f.read_text(encoding="utf-8", errors="ignore"))]
    d_shell = ys.Design()
    ys.run_pass(f"read_verilog {shell_v}", d_shell)
    shell_top_only = bench_work / ".tmp_shell_top_only.v"
    ys.run_pass(f"select -module {top_mod}; write_verilog -selected {shell_top_only}; select -clear", d_shell)
    extra = []
    d_approx = ys.Design()
    ys.run_pass(f"read_verilog -sv {approx_cone_v}", d_approx)
    approx_mods = set(m.str().lstrip("\\") for m in d_approx.modules_.keys())
    d_extracted = ys.Design()
    ys.run_pass(f"read_verilog -sv {cone_v}", d_extracted)
    missing = set(m.str().lstrip("\\") for m in d_extracted.modules_.keys()) - approx_mods
    if missing:
        tmp = bench_work / ".tmp_extra_cones.v"
        sel = " ".join(f"\\{m}" for m in sorted(missing))
        ys.run_pass(f"select {sel}; write_verilog -selected {tmp}; select -clear", d_extracted)
        if tmp.exists():
            extra.append(tmp.read_text(encoding="utf-8", errors="ignore"))
            tmp.unlink()
    parts = untouched + extra + [shell_top_only.read_text(encoding="utf-8", errors="ignore"),
                                 approx_cone_v.read_text(encoding="utf-8", errors="ignore")]
    final_approx_top_v.write_text("\n\n".join(parts), encoding="utf-8")
    shell_top_only.unlink(missing_ok=True)


def _formal_control(exact_top_v: Path, approx_top_v: Path, meta_json: Optional[Path]) -> str:
    try:
        return FormalControlVerifier(exact_vlog=exact_top_v, approx_vlog=approx_top_v, meta_json=meta_json,
                                     timeout=FORMAL_TIMEOUT_S).verify().get("status", "ERROR")
    except Exception as exc:
        logger.warning("Formal control check error: %s", exc)
        return "ERROR"


def _formal_checks(exact_top_v: Path, approx_top_v: Path, cone_v: Path, approx_cone_v: Path,
                   meta_json: Optional[Path], top_mod: str) -> Tuple[str, str]:
    """Yosys SAT: control-output equivalence of the tops, WCE bound of the cones."""
    try:
        ctrl = FormalControlVerifier(exact_vlog=exact_top_v, approx_vlog=approx_top_v, meta_json=meta_json,
                                     timeout=FORMAL_TIMEOUT_S).verify().get("status", "ERROR")
    except Exception as exc:
        logger.warning("Formal control check error: %s", exc)
        ctrl = "ERROR"
    try:
        wce = FormalWceVerifier(exact_cone_vlog=cone_v, approx_cone_vlog=approx_cone_v, meta_json=meta_json,
                                timeout=FORMAL_TIMEOUT_S, signed_outputs="sub" in top_mod.lower()).verify().get("status", "ERROR")
    except Exception as exc:
        logger.warning("Formal WCE check error: %s", exc)
        wce = "ERROR"
    return ctrl, wce


def _util(res: Dict[str, Any], power: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "luts": res["total_luts"],
        "ffs": res["ffs"],
        "dsps": res["dsps"],
        "brams": res.get("brams", 0),
        "ramb36": res.get("ramb36", 0),
        "ramb18": res.get("ramb18", 0),
        "carry4": res["carry4"],
        "logic_cells": res["estimated_lcs"],
        "lut_util_pct": res["lut_utilization_pct"],
        "ff_util_pct": res["ff_utilization_pct"],
        "dsp_util_pct": res.get("dsp_utilization_pct", 0.0),
        "bram_util_pct": res.get("bram_utilization_pct", 0.0),
        "dynamic_power_mw": power["dynamic_power_mw"],
        "power_breakdown": power["breakdown"],
    }


def _pct_saved(a: float, b: float) -> float:
    return round((a - b) / a * 100.0, 2) if a else 0.0


@contextlib.contextmanager
def _stage_pool(workers: int):
    """Thread pool plus a cancel flag; on exit pending work is dropped and running work is awaited."""
    cancel = threading.Event()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        yield pool, cancel
    finally:
        cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)


def run_benchmark(bench: Dict[str, Any], estimator: FpgaEstimator, inner_jobs: int = 1) -> Dict[str, Any]:
    """Run the full Yosys + pyALS flow, verification, application metric and power on one benchmark.

    ``inner_jobs`` > 1 evaluates candidates concurrently (subprocess steps in parallel, in-process
    pyosys steps serialized). The result is identical to ``inner_jobs`` = 1.
    """
    bench_id = bench["id"]
    top_mod = bench["top"]
    logger.info("=" * 80)
    logger.info("Benchmark: %s (%s) on %s | inner jobs: %d", bench["name"], bench_id, FPGA_PART, inner_jobs)
    logger.info("=" * 80)

    bench_work = BUILD_DIR / bench_id
    if bench_work.exists():
        shutil.rmtree(bench_work)
    bench_work.mkdir(parents=True, exist_ok=True)
    bench = dict(bench, files=_patched_files(bench, bench_work))

    exact_top_v = bench_work / "exact_baseline_top.v"
    exact_top_v.write_text("\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in bench["files"]), encoding="utf-8")

    # Application stimulus (shared by every attempt)
    ports = _top_ports(bench["files"], top_mod)
    vectors, ctx = axbench_apps.build_stimulus(bench_id)
    app_dir = bench_work / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    sim_kw = dict(top=top_mod, inputs=ports["inputs"], outputs=ports["outputs"], work_dir=app_dir,
                  clk=ports["clk"], rst=ports["rst"], rst_active_low=ports["rst_active_low"])
    skip = 16 if ports["clk"] else 0
    med_limit, mred_limit = bench["med_limit"], bench["mred_limit"]

    # Stage 1: exact baseline synthesis and exact application reference. Both are subprocess-only,
    # so they overlap with the cone extraction (pyosys, main thread) below.
    logger.info("[Stage 1] Yosys synth_xilinx of exact baseline...")
    exact_map_v, exact_map_j = bench_work / "exact_mapped.v", bench_work / "exact_mapped.json"

    def exact_reference() -> Dict[str, Any]:
        norm = _normalize_rtl(exact_top_v, top_mod, app_dir / "exact_norm.v")
        return run_vector_sim([norm], vectors=vectors, tag="app_exact", **sim_kw)

    pre = ThreadPoolExecutor(max_workers=2 if inner_jobs > 1 else 1)
    f_synth = pre.submit(estimator.evaluate_netlist, exact_top_v, top_module=top_mod, work_dir=bench_work,
                         out_mapped_vlog=exact_map_v, out_mapped_json=exact_map_j)
    f_sim = pre.submit(exact_reference) if inner_jobs > 1 else None

    # Stage 2: Attribute Annotation & Safe Cone Extraction
    logger.info("[Stage 2] Annotating Verilog attributes and extracting safe combinational cone...")
    annotated_dir = bench_work / "annotated_rtl"
    annotated_files = annotate_verilog(
        input_files=bench["files"],
        output_dir=annotated_dir,
        top_module=top_mod,
        relax_wire=bench["relax_wire"],
        precise_signals=bench["precise_signals"],
        bound=bench["bound"],
        metric=bench["metric"],
    )

    cone_v = bench_work / "extracted_cone.v"
    shell_v = bench_work / "top_shell.v"
    meta_json = bench_work / "cone_meta.json"

    extractor = ConeExtractor(annotated_files, top_module=top_mod)
    meta = extractor.extract_safe_cone(
        submod_tag=f"{top_mod}_axc_cone_0",
        out_submod_path=cone_v,
        out_top_path=shell_v,
        out_meta_path=meta_json,
    )
    logger.info("Cone extraction status: %s | Safe cells: %d", meta.get("status"), len(meta.get("safe_cells", [])))

    approx_cone_v = bench_work / "champion_approx_cone.v"
    final_approx_top_v = bench_work / "final_approx_top.v"
    approx_map_v, approx_map_j = bench_work / "approx_mapped.v", bench_work / "approx_mapped.json"
    has_safe_cone = cone_v.exists() and len(meta.get("safe_cells", [])) > 0 and meta.get("status") not in ("NO_RELAX_SIGNALS", "TAINTED_EMPTY_SAFE_SET")
    cone_meta = meta_json if meta_json.exists() else None

    exact_res = f_synth.result()
    exact_sim = f_sim.result() if f_sim else exact_reference()
    pre.shutdown()
    logger.info("Exact: LUT=%d FF=%d DSP=%d BRAM=%d CARRY4=%d LC=%d", exact_res["total_luts"], exact_res["ffs"],
                exact_res["dsps"], exact_res["brams"], exact_res["carry4"], exact_res["estimated_lcs"])

    attempts: List[Dict[str, Any]] = []
    accepted: Optional[Dict[str, Any]] = None
    fallback_reason: Optional[str] = None

    def assess(top_v: Path, label: str, variant: str, map_v: Path, map_j: Path, need_savings_first: bool,
               cancel: threading.Event, key: str) -> Dict[str, Any]:
        """Synthesize + check one candidate design. Errors are measured on the application stream,
        in the relaxed output's number format. Formal proofs are run by the caller.
        Thread-safe: every output file name carries ``key``."""
        rec: Dict[str, Any] = {"candidate": label, "variant": variant}

        def checkpoint() -> None:
            if cancel.is_set():
                raise Cancelled(label)

        checkpoint()
        a_res = estimator.evaluate_netlist(top_v, top_module=top_mod, work_dir=bench_work,
                                           out_mapped_vlog=map_v, out_mapped_json=map_j)
        rec.update(lcs=a_res["estimated_lcs"], luts=a_res["total_luts"], _res=a_res)
        rec["savings_ok"] = a_res["estimated_lcs"] < exact_res["estimated_lcs"] or a_res["total_luts"] < exact_res["total_luts"]
        no_sav = (f"no top-level savings (LC {exact_res['estimated_lcs']}->{a_res['estimated_lcs']}, "
                  f"LUT {exact_res['total_luts']}->{a_res['total_luts']})")
        if need_savings_first and not rec["savings_ok"]:
            rec.update(outcome="rejected", reason=no_sav, err_ok=None)
            logger.info("  %s [%s]: rejected: %s", label, variant, no_sav)
            return rec
        checkpoint()
        miter = MiterVerifier(exact_vlog=exact_top_v, approx_vlog=top_v, meta_json=cone_meta,
                              num_cycles=1000, enable_formal=False, sim_timeout=SIM_TIMEOUT_S).verify()
        checkpoint()
        norm = _normalize_rtl(top_v, top_mod, app_dir / f"approx_norm_{key}.v")
        sim = run_vector_sim([norm], vectors=vectors, tag=f"app_approx_{key}", **sim_kw)
        errs = axbench_apps.error_stats(bench_id, exact_sim["outputs"], sim["outputs"], bench["relax_wire"], skip)
        app = axbench_apps.app_metrics(bench_id, vectors, exact_sim["outputs"], sim["outputs"],
                                       relax_wire=bench["relax_wire"], skip=skip)
        fails = []
        if miter.get("control_integrity") != "PASSED":
            fails.append("control outputs corrupted")
        if errs["wce"] > bench["bound"]:
            fails.append(f"WCE {errs['wce']:g} > {bench['bound']:g}")
        if med_limit is not None and errs["med"] > med_limit:
            fails.append(f"MED {errs['med']:.3f} > {med_limit:g}")
        if mred_limit is not None and errs["mred"] > mred_limit:
            fails.append(f"MRED {errs['mred'] * 100:.4f}% > {mred_limit * 100:g}%")
        if app["pass"] is False:
            fails.append(f"{app['name']} {_fmt(app['value'], 3)} {app['unit']} fails {app['threshold']}")
        rec.update(wce=errs["wce"], med=errs["med"], mred_pct=errs["mred"] * 100.0, app_value=app["value"],
                   err_ok=not fails, _errs=errs, _app=app)
        if fails or not rec["savings_ok"]:
            rec.update(outcome="rejected", reason="; ".join(fails + ([] if rec["savings_ok"] else [no_sav])))
            logger.info("  %s [%s]: rejected: %s", label, variant, rec["reason"])
        else:
            rec.update(outcome="passed checks")
        return rec

    def finalize(rec: Dict[str, Any], top_v: Path, map_v: Path, map_j: Path, formal_ctrl: str, formal_wce: str) -> Dict[str, Any]:
        rec.update(outcome="accepted", formal_control=formal_ctrl, formal_wce=formal_wce)
        if top_v != final_approx_top_v:
            shutil.copy(top_v, final_approx_top_v)
        if map_v != approx_map_v:
            shutil.copy(map_v, approx_map_v)
            shutil.copy(map_j, approx_map_j)
        e = rec["_errs"]
        logger.info("  ACCEPTED %s: LC %d -> %d | WCE=%g MED=%.3f MRED=%.4f%% | %s=%s | formal %s/%s", rec["candidate"],
                    exact_res["estimated_lcs"], rec["lcs"], e["wce"], e["med"], e["mred"] * 100,
                    rec["_app"]["name"], _fmt(rec["_app"]["value"], 3), formal_ctrl, formal_wce)
        return {"name": rec["candidate"] + (f" [{rec['variant']}]" if rec["variant"] in CONE_VARIANTS else ""),
                "res": rec["_res"], "errs": e, "app": rec["_app"], "formal_ctrl": formal_ctrl, "formal_wce": formal_wce}

    # Stage 3a: design-level partial-product pruning of every array multiplier (UB1BPPG cells), before flattening.
    # Binary search over the anti-diagonal depth; with inner jobs the next two possible depths are evaluated
    # speculatively. The visited depths and the choice are identical to the serial search.
    exact_code = exact_top_v.read_text(encoding="utf-8", errors="ignore")
    ppgs = PpgPruner.detect_ppg_modules(exact_code)
    if ppgs:
        ppg_dir = bench_work / "ppg"
        ppg_dir.mkdir(parents=True, exist_ok=True)
        pruner = PpgPruner(work_dir=ppg_dir)
        k_max = max(i + j for i, j in ppgs) + 1
        logger.info("[Stage 3a] Design-level PPG pruning: %d UB1BPPG cells, anti-diagonal depth search in [1, %d]", len(ppgs), k_max)

        def eval_depth(k: int, cancel: threading.Event) -> Dict[str, Any]:
            code_k, n_pruned = pruner.prune_code_ppg(exact_code, k)
            top_k = ppg_dir / f"design_ppg_k{k}.v"
            top_k.write_text(code_k, encoding="utf-8")
            rec = assess(top_k, f"ppg_design_k{k}", "design", ppg_dir / f"k{k}_mapped.v", ppg_dir / f"k{k}_mapped.json",
                         need_savings_first=False, cancel=cancel, key=f"ppg{k}")
            rec.update(pruned_ppgs=n_pruned, _top=top_k)
            return rec

        with _stage_pool(inner_jobs) as (pool, cancel):
            visited, lo = depth_search(k_max, MAX_PPG_EVALS, lambda k: eval_depth(k, cancel), lambda r: bool(r["err_ok"]),
                                       executor=pool if inner_jobs > 1 else None, speculate=min(2, inner_jobs - 1))
        evaluated: Dict[int, Dict[str, Any]] = {}
        for k, rec in visited:
            attempts.append(rec)
            evaluated[k] = rec
        # Synthesized size is not monotone in k (pruned zeros reshape the Wallace tree), so pick the
        # cheapest evaluated depth that passed every check, not simply the deepest in-budget one.
        passing = [(r["lcs"], r["luts"], -k, k) for k, r in evaluated.items() if r["err_ok"] and r["savings_ok"]]
        best_k = min(passing)[3] if passing else None
        best = evaluated.get(best_k) if best_k is not None else None
        lo = best_k if best_k is not None else lo
        if best is not None:
            formal_ctrl = _formal_control(exact_top_v, best["_top"], cone_meta)
            if formal_ctrl == "FALSIFIED":
                best.update(outcome="rejected", reason="formal control=FALSIFIED")
            else:
                accepted = finalize(best, best["_top"], ppg_dir / f"k{lo}_mapped.v", ppg_dir / f"k{lo}_mapped.json",
                                    formal_ctrl, "N/A (design-level PPG pruning; errors measured on application stream)")
        else:
            logger.info("  no evaluated PPG depth passed every check")

    # Stage 3b: pyALS Candidate Tournament on the extracted Cone
    if accepted is None and not has_safe_cone:
        logger.warning("No safe cells extracted for %s (status: %s)", top_mod, meta.get("status"))
        fallback_reason = "no safe cone extracted" + (" and no in-budget PPG depth with savings" if ppgs else "")
    elif accepted is None:
        logger.info("[Stage 3b] pyALS Candidate Tournament on the safe cone...")
        pyals_dir = bench_work / "pyals_run"
        bridge = PyAlsBridge(cone_verilog_path=cone_v, meta_json_path=cone_meta, work_dir=pyals_dir, cut_size=4)
        tournament = Tournament(estimator=FpgaEstimator(family="xc7", device=FPGA_DEVICE, nodsp=False),
                                work_dir=pyals_dir, sim_samples=64)
        champion = tournament.run(cone_vlog=cone_v, meta_json=cone_meta, metric=bench["metric"],
                                  bound=bench["bound"], generators=[bridge])
        alts = list(champion.alternatives)[:MAX_ATTEMPTS]
        logger.info("Tournament: %d qualifying candidates (trying up to %d)", len(champion.alternatives), len(alts))
        exact_cone_norm = pyals_dir / "exact_cone_clean.v"
        att_root = bench_work / "attempts"

        def attempt(job: Tuple[int, Any, str], cancel: threading.Event) -> Dict[str, Any]:
            """One (candidate, variant) attempt in its own directory: reintegrate, assess, formal proofs."""
            idx, alt, variant = job
            adir = att_root / f"a{idx:02d}"
            adir.mkdir(parents=True, exist_ok=True)
            logger.info("[Attempt] %s [%s] (cone LCs %d -> %d)", alt.name, variant, champion.baseline_lcs, alt.lcs)
            cone_a, top_a = adir / "approx_cone.v", adir / "final_top.v"
            map_v, map_j = adir / "approx_mapped.v", adir / "approx_mapped.json"
            # "raw": candidate as simulated by the Tournament (final synth optimizes across the seam);
            # "remapped": cone pre-mapped to Xilinx primitives.
            src = alt.netlist_path if variant == "raw" else tournament.remap(alt.netlist_path, out_path=adir / "remapped_cone.v")
            shutil.copy(src, cone_a)
            _reintegrate(bench, adir, shell_v, cone_v, cone_a, top_a)
            rec = assess(top_a, alt.name, variant, map_v, map_j, need_savings_first=True, cancel=cancel, key=f"a{idx:02d}")
            rec.update(cone_lcs=alt.lcs, cone_baseline_lcs=champion.baseline_lcs, _paths=(top_a, map_v, map_j, cone_a))
            if rec.get("outcome") != "passed checks":
                return rec
            # Exact side of the WCE proof = the Tournament's normalized cone (raw extracted cone can leave
            # relaxed-bus bits undriven, which SAT treats as free).
            formal_ctrl, formal_wce = _formal_checks(exact_top_v, top_a,
                                                     exact_cone_norm if exact_cone_norm.exists() else cone_v,
                                                     cone_a, cone_meta, top_mod)
            rec["_formal"] = (formal_ctrl, formal_wce)
            if "FALSIFIED" in (formal_ctrl, formal_wce):
                rec.update(outcome="rejected", reason=f"formal control={formal_ctrl}, WCE={formal_wce}")
                logger.info("  rejected: %s", rec["reason"])
            return rec

        jobs = [(i, alt, variant) for i, (alt, variant) in enumerate((a, v) for a in alts for v in CONE_VARIANTS)]
        with _stage_pool(inner_jobs) as (pool, cancel):
            consumed, found = first_accepted(jobs, lambda j: attempt(j, cancel), lambda r: r.get("outcome") == "passed checks",
                                             executor=pool if inner_jobs > 1 else None, cancel=cancel)
        attempts.extend(rec for _, rec in consumed)
        if found is not None:
            rec = consumed[found][1]
            top_a, map_v, map_j, cone_a = rec["_paths"]
            shutil.copy(cone_a, approx_cone_v)
            accepted = finalize(rec, top_a, map_v, map_j, *rec["_formal"])
        if accepted is None:
            fallback_reason = (f"none of {len(alts)} cone candidate(s) x {len(CONE_VARIANTS)} variant(s)"
                               + (" or PPG depths" if ppgs else "") + " met every threshold (see attempts)") if alts else \
                              ("no cone candidate met the cone bound with resource savings" + (" and no in-budget PPG depth with savings" if ppgs else ""))

    for r in attempts:   # drop internal objects before export
        for k in [k for k in r if k.startswith("_")]:
            r.pop(k)

    fell_back = accepted is None
    if fell_back:
        logger.warning("Keeping exact design: %s", fallback_reason)
        shutil.copy(exact_top_v, final_approx_top_v)
        shutil.copy(exact_map_v, approx_map_v)
        shutil.copy(exact_map_j, approx_map_j)
        approx_res = dict(exact_res)
        method = "exact (fallback)"
        errs = {"wce": 0.0, "med": 0.0, "mred": 0.0, "er": 0.0}
        app = axbench_apps.app_metrics(bench_id, vectors, exact_sim["outputs"], exact_sim["outputs"],
                                       relax_wire=bench["relax_wire"], skip=skip)
        formal_ctrl = formal_wce = "N/A (exact design kept)"
    else:
        approx_res, method, errs, app = accepted["res"], accepted["name"], accepted["errs"], accepted["app"]
        formal_ctrl, formal_wce = accepted["formal_ctrl"], accepted["formal_wce"]
    app["note"] = ctx.get("note", "")
    app["samples"] = len(vectors)

    # Stage 8: activity-based power of mapped netlists
    logger.info("[Stage 8] Activity-based dynamic power @ %.0f MHz...", F_CLK_MHZ)
    pvecs = axbench_apps.power_vectors(bench_id, vectors)
    pwr_dir = bench_work / "power"
    pk = dict(sim_kw, work_dir=pwr_dir, vcd=True)

    def power_of(map_v: Path, map_j: Path, tag: str) -> Dict[str, Any]:
        ps = run_vector_sim([map_v], vectors=pvecs, tag=tag, **pk)
        return estimate_dynamic_power(map_j, top_mod, ps["vcd"], ps["cycles"], F_CLK_MHZ)

    with ThreadPoolExecutor(max_workers=2 if inner_jobs > 1 else 1) as pp:
        f_e = pp.submit(power_of, exact_map_v, exact_map_j, "pwr_exact")
        f_a = None if fell_back else pp.submit(power_of, approx_map_v, approx_map_j, "pwr_approx")
        exact_pwr = f_e.result()
        approx_pwr = exact_pwr if fell_back else f_a.result()
    for f in pwr_dir.glob("*.vcd"):
        f.unlink()
    logger.info("Power: exact %.3f mW -> approx %.3f mW", exact_pwr["dynamic_power_mw"], approx_pwr["dynamic_power_mw"])

    eu, au = _util(exact_res, exact_pwr), _util(approx_res, approx_pwr)
    return {
        "id": bench_id,
        "name": bench["name"],
        "domain": bench["domain"],
        "top": top_mod,
        "device": FPGA_PART,
        "engine": "pyALS Candidate Tournament",
        "method": method,
        "fell_back": fell_back,
        "fallback_reason": fallback_reason,
        "metric": bench["metric"],
        "bound": bench["bound"],
        "exact": eu,
        "approx": au,
        "savings_pct": {k: _pct_saved(eu[k], au[k]) for k in ("luts", "ffs", "dsps", "brams", "carry4", "logic_cells", "dynamic_power_mw")},
        "errors": {
            "wce": errs["wce"],
            "med": round(errs["med"], 4),
            "mred_pct": round(errs["mred"] * 100.0, 4),
            "er": round(errs["er"], 4),
            "domain": "application stream, relaxed output in its number format (LSB units)",
        },
        "formal": {"control_equivalence": formal_ctrl, "wce_bound": formal_wce},
        "limits": {"wce": bench["bound"], "med": med_limit, "mred_pct": None if mred_limit is None else mred_limit * 100.0,
                   "app": app["threshold"]},
        "attempts": attempts,
        "app": app,
        "power_model": {k: exact_pwr[k] for k in ("method", "f_clk_mhz", "energy_pj")} | {"cycles": exact_pwr["cycles"]},
    }


def _fmt(v: Any, nd: int = 2) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "inf"
        return f"{v:.{nd}f}"
    return str(v)


def export_results(results: List[Dict[str, Any]], out_dir: Path) -> None:
    """Write results.json, results.csv and report.md into out_dir (merging earlier benchmarks)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, csv_path, md_path = out_dir / "results.json", out_dir / "results.csv", out_dir / "report.md"

    merged: Dict[str, Dict[str, Any]] = {}
    if json_path.exists():
        try:
            for r in json.loads(json_path.read_text()):
                merged[r["id"]] = r
        except Exception:
            pass
    for r in results:
        merged[r["id"]] = r
    order = [b["id"] for b in define_benchmark_suite()]
    results = sorted(merged.values(), key=lambda r: order.index(r["id"]) if r["id"] in order else 99)

    json_path.write_text(json.dumps(results, indent=2, default=lambda o: None if isinstance(o, float) and math.isinf(o) else str(o)))

    res_keys = ["luts", "ffs", "dsps", "brams", "carry4", "logic_cells", "dynamic_power_mw"]
    headers = ["id", "name", "method", "fell_back", "bound"]
    for k in res_keys:
        headers += [f"exact_{k}", f"approx_{k}", f"saved_{k}_pct"]
    headers += ["wce", "med", "mred_pct", "er", "formal_control", "formal_wce", "app_metric", "app_value", "app_unit", "app_threshold", "app_pass"]
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in results:
            row = [r["id"], r["name"], r["method"], r["fell_back"], r["bound"]]
            for k in res_keys:
                row += [r["exact"][k], r["approx"][k], r["savings_pct"][k]]
            e, a = r["errors"], r["app"]
            row += [e["wce"], e["med"], e["mred_pct"], e["er"], r["formal"]["control_equivalence"], r["formal"]["wce_bound"],
                    a["name"], _fmt(a["value"], 4), a["unit"], a["threshold"], a["pass"]]
            w.writerow(row)

    pm = results[0]["power_model"] if results else {}
    md: List[str] = [
        "# AxFPGA AxBench Evaluation: Zybo Z7-10 (xc7z010clg400-1)",
        "",
        "Synthesis: Yosys `synth_xilinx -family xc7 -flatten -noiopad` (DSP inference allowed). "
        "Approximation: pyALS Candidate Tournament. LC = LUT + 2 x CARRY4. "
        "Utilization % is relative to the xc7z010 (17,600 LUT, 35,200 FF, 80 DSP48E1, 60 BRAM36).",
        "",
        "## 1. Utilization: baseline vs approximate",
        "",
        "| Benchmark | Champion | LUT | FF | DSP | BRAM | CARRY4 | LC | LC saved |",
        "|:--|:--|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for r in results:
        e, a, s = r["exact"], r["approx"], r["savings_pct"]
        cell = lambda k: f"{e[k]} → {a[k]}"
        md.append(f"| **{r['name']}** | `{r['method']}` | {cell('luts')} | {cell('ffs')} | {cell('dsps')} | {cell('brams')} | {cell('carry4')} | {cell('logic_cells')} | **{s['logic_cells']:.1f}%** |")
    md += ["", "Device utilization % (baseline → approximate):", "",
           "| Benchmark | LUT % | FF % | DSP % | BRAM % |", "|:--|--:|--:|--:|--:|"]
    for r in results:
        e, a = r["exact"], r["approx"]
        md.append(f"| {r['name']} | {e['lut_util_pct']:.2f} → {a['lut_util_pct']:.2f} | {e['ff_util_pct']:.2f} → {a['ff_util_pct']:.2f} | "
                  f"{e['dsp_util_pct']:.2f} → {a['dsp_util_pct']:.2f} | {e['bram_util_pct']:.2f} → {a['bram_util_pct']:.2f} |")

    md += ["", f"## 2. Estimated dynamic power @ {pm.get('f_clk_mhz', F_CLK_MHZ):.0f} MHz", "",
           "| Benchmark | Baseline (mW) | Approximate (mW) | Saved |", "|:--|--:|--:|--:|"]
    for r in results:
        md.append(f"| {r['name']} | {r['exact']['dynamic_power_mw']:.3f} | {r['approx']['dynamic_power_mw']:.3f} | {r['savings_pct']['dynamic_power_mw']:.1f}% |")
    md += ["", "**Power model (estimate, not sign-off):** zero-delay iverilog simulation of each Yosys-mapped netlist with the "
           f"benchmark's application stimulus ({pm.get('cycles', '?')} cycles max), toggle counts from VCD, "
           "P = sum E_toggle x toggle rate x f_clk. Glitch power and static power are not modelled "
           "(static power is a device constant, identical for baseline and approximate). Energy assumptions (pJ, VCCINT = 1.0 V): "
           + ", ".join(f"{k} {v}" for k, v in pm.get("energy_pj", {}).items()) + "."]

    md += ["", "## 3. Error metrics and formal verification", "",
           "Errors are measured on each benchmark's application stimulus, with the relaxed output decoded in its number format "
           "(two's complement / sign-magnitude, LSB units). Limits come from the application tolerance (see ERROR_BUDGET). "
           f"Every reported approximation satisfies WCE <= limit, MED <= limit (default WCE/2), MRED <= limit (default {MRED_LIMIT * 100:g} %), "
           "its application threshold, intact control outputs, and no FALSIFIED formal proof. "
           "Designs with array multipliers first try design-level partial-product pruning (largest in-budget anti-diagonal depth, "
           f"binary search, {MAX_PPG_EVALS} steps max); otherwise cone candidates are tried cheapest first (up to {MAX_ATTEMPTS}, each "
           f"reintegrated as {' then '.join(CONE_VARIANTS)}). If none passes, the exact design is reported.", "",
           "| Benchmark | WCE (limit) | MED (limit) | MRED % (limit) | ER | Formal control | Formal WCE |",
           "|:--|--:|--:|--:|--:|:--|:--|"]
    for r in results:
        e, f, lim = r["errors"], r["formal"], r["limits"]
        md.append(f"| {r['name']} | {e['wce']:.0f} ({lim['wce']:g}) | {e['med']:.3f} ({lim['med']:g}) | {e['mred_pct']:.4f} ({'n/a' if lim['mred_pct'] is None else format(lim['mred_pct'], 'g')}) | "
                  f"{e['er']:.3f} | {f['control_equivalence']} | {f['wce_bound']} |")

    md += ["", "## 4. Application-specific metrics (exact vs approximate hardware)", "",
           "| Benchmark | Metric | Value | Threshold | Status | Details |", "|:--|:--|--:|:--|:--|:--|"]
    for r in results:
        a = r["app"]
        status = {True: "PASS", False: "FAIL", None: "n/a"}[a["pass"]]
        extra = ", ".join(f"{k}={_fmt(v, 4)}" for k, v in a.get("extra", {}).items())
        md.append(f"| {r['name']} | {a['name']} | {_fmt(a['value'], 3)} {a['unit']} | {a['threshold']} | {status} | {extra} |")
    md += ["", "Stimulus per benchmark:", ""]
    for r in results:
        md.append(f"- **{r['name']}**: {r['app']['note']} ({r['app']['samples']} samples)")

    md += ["", "## 5. Candidate attempts", ""]
    for r in results:
        head = f"exact design kept: {r['fallback_reason']}" if r["fell_back"] else f"accepted `{r['method']}`"
        md.append(f"- **{r['name']}**: {head}")
        for a in r.get("attempts", []):
            why = "accepted" if a.get("outcome") == "accepted" else a.get("reason", "")
            md.append(f"  - `{a['candidate']}` [{a.get('variant', 'remapped')}]: {why}")
    md_path.write_text("\n".join(md) + "\n")
    logger.info("Wrote %s, %s, %s", json_path, csv_path, md_path)


def _mem_available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 1e9


def _mem_total_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 16.0


def _inner_jobs_for(bench_id: str, inner_jobs: int, budget_gb: float) -> int:
    """Concurrent candidate evaluations for one benchmark: at most budget/2 of memory for itself."""
    gb = EST_GB.get(bench_id, DEFAULT_GB)
    return max(1, min(inner_jobs, int((budget_gb * 0.5 - BASE_GB) // gb)))


def _benchmark_process(bench_id: str, build_dir: str, nodsp: bool, cone_variants: Tuple[str, ...],
                       inner_jobs: int, result_path: str) -> None:
    """Entry point of one benchmark process (own pyosys state, own memory)."""
    global BUILD_DIR, CONE_VARIANTS
    os.setpgrp()                      # own process group: the scheduler can stop this benchmark and all its children
    BUILD_DIR = Path(build_dir)
    CONE_VARIANTS = tuple(cone_variants)
    for h in logging.getLogger().handlers:
        h.setFormatter(logging.Formatter(f"[%(asctime)s] [{bench_id}] [%(levelname)s] %(name)s: %(message)s"))
    bench = next(b for b in define_benchmark_suite() if b["id"] == bench_id)
    t0 = time.time()
    result = run_benchmark(bench, FpgaEstimator(family="xc7", device=FPGA_DEVICE, nodsp=nodsp), inner_jobs)
    stats = {"id": bench_id, "wall_s": round(time.time() - t0, 1), "inner_jobs": inner_jobs,
             "peak_child_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1),
             "peak_self_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
    (BUILD_DIR / bench_id / "stats.json").write_text(json.dumps(stats, indent=2))
    with open(result_path, "wb") as f:
        pickle.dump(result, f)


def run_suite(suite: List[Dict[str, Any]], out_dir: Path, jobs: int, inner_jobs: int, budget_gb: float,
              nodsp: bool) -> List[str]:
    """Run benchmarks as separate processes, longest first, within a memory budget. Returns failed ids."""
    ctx = mp.get_context("spawn")
    pending = sorted(suite, key=lambda b: -EST_COST.get(b["id"], 5))
    running: Dict[str, Tuple[Any, float, Path, float]] = {}   # id -> (process, planned_gb, result_path, start)
    failed: List[str] = []
    hold_until = 0.0                    # after a memory kill nothing new is admitted for HOLD_S seconds
    forced_inner: Dict[str, int] = {}   # benchmarks requeued by the memory watchdog run with fewer inner jobs
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Scheduler: %d benchmark(s), up to %d at once, memory budget %.1f GB, inner jobs <= %d",
                len(pending), jobs, budget_gb, inner_jobs)

    def reap(bench_id: str, proc: Any, rpath: Path, t_start: float) -> None:
        proc.join()
        dt = time.time() - t_start
        if proc.exitcode == 0 and rpath.exists():
            with open(rpath, "rb") as f:
                result = pickle.load(f)
            export_results([result], out_dir)
            logger.info("[scheduler] %s done in %.0f s", bench_id, dt)
        else:
            logger.error("[scheduler] %s FAILED (exit code %s) after %.0f s", bench_id, proc.exitcode, dt)
            failed.append(bench_id)

    while pending or running:
        for b in list(pending):
            if len(running) >= jobs or (running and time.time() < hold_until):
                break
            ij = min(_inner_jobs_for(b["id"], inner_jobs, budget_gb), forced_inner.get(b["id"], inner_jobs))
            gb = BASE_GB + EST_GB.get(b["id"], DEFAULT_GB) * ij
            planned = sum(v[1] for v in running.values())
            if running and (planned + gb > budget_gb or gb > _mem_available_gb() - RESERVE_GB):
                continue                      # a lighter benchmark further down the queue may still fit
            pending.remove(b)
            rpath = BUILD_DIR / f"{b['id']}.result.pkl"
            rpath.unlink(missing_ok=True)
            proc = ctx.Process(target=_benchmark_process, name=f"bench-{b['id']}",
                               args=(b["id"], str(BUILD_DIR), nodsp, CONE_VARIANTS, ij, str(rpath)))
            proc.start()
            running[b["id"]] = (proc, gb, rpath, time.time())
            logger.info("[scheduler] started %s (inner jobs %d, planned %.1f GB; running: %s)", b["id"], ij, gb, sorted(running))
        if len(running) > 1 and _mem_available_gb() < KILL_BELOW_GB:
            victim = max(running, key=lambda i: running[i][3])        # newest benchmark loses the least work
            proc, _, rpath, _ = running.pop(victim)
            logger.error("[scheduler] memory low (%.1f GB available): stopping %s, requeued with 1 inner job",
                         _mem_available_gb(), victim)
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            proc.join()
            forced_inner[victim] = 1
            hold_until = time.time() + HOLD_S
            pending.insert(0, next(b for b in suite if b["id"] == victim))
        for bench_id in [i for i, v in running.items() if not v[0].is_alive()]:
            proc, _, rpath, t_start = running.pop(bench_id)
            reap(bench_id, proc, rpath, t_start)
            hold_until = 0.0
        time.sleep(2)
    return failed


def main():
    global BUILD_DIR, CONE_VARIANTS
    parser = argparse.ArgumentParser(description="AxBench evaluation of AxFPGA on Zybo Z7-10 (Yosys only)")
    parser.add_argument("--benchmark", "-b", action="append", default=None, help="Benchmark id (repeatable); default all")
    parser.add_argument("--out-dir", "-o", default=str(REPORTS_DIR), help="Directory for results.json/csv and report.md")
    parser.add_argument("--build-dir", default=str(BUILD_DIR), help="Working directory for per-benchmark artifacts")
    parser.add_argument("--nodsp", action="store_true", help="Disable DSP48E1 inference")
    parser.add_argument("--cone-variants", default=",".join(CONE_VARIANTS),
                        help="Comma-separated reintegration variants tried per candidate, in order: raw, remapped")
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="Benchmarks run at once, each in its own process (default 1 = serial, in-process)")
    parser.add_argument("--inner-jobs", type=int, default=INNER_JOBS,
                        help="Concurrent candidate evaluations inside one benchmark (1 = serial); results are identical")
    parser.add_argument("--mem-gb", type=float, default=None,
                        help="Memory budget for the scheduler in GB (default 70%% of RAM)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    BUILD_DIR = Path(args.build_dir).resolve()
    CONE_VARIANTS = tuple(v.strip() for v in args.cone_variants.split(",") if v.strip() in ("raw", "remapped")) or ("remapped",)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    budget_gb = args.mem_gb if args.mem_gb else _mem_total_gb() * 0.7

    suite = define_benchmark_suite()
    if args.benchmark:
        suite = [b for b in suite if b["id"] in args.benchmark or b["top"] in args.benchmark]
        if not suite:
            logger.error("No benchmark matches %s", args.benchmark)
            sys.exit(2)

    t0 = time.time()
    failed: List[str] = []
    if args.jobs <= 1:
        estimator = FpgaEstimator(family="xc7", device=FPGA_DEVICE, nodsp=args.nodsp)
        for bench in suite:
            try:
                export_results([run_benchmark(bench, estimator, max(1, args.inner_jobs))], out_dir)
            except Exception as exc:
                logger.exception("Benchmark %s failed: %s", bench["id"], exc)
                failed.append(bench["id"])
    else:
        failed = run_suite(suite, out_dir, args.jobs, max(1, args.inner_jobs), budget_gb, args.nodsp)
    logger.info("Done in %.1f s. Failed: %s", time.time() - t0, failed or "none")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
