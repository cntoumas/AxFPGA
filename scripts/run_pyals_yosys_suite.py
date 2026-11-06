#!/usr/bin/env python3
"""Benchmark-suite evaluation of AxFPGA on the Zybo Z7-10 (xc7z010clg400-1), Yosys only.

Designs (--group): the 13 AxBench designs ("axbench") and the Phase-4 set ("phase4": the behavioural-multiplier
variants of the AxBench multiplier designs and four larger designs written for this repository, benchmarks/*/).
Designs larger than the xc7z010 are evaluated on the xc7z020 and labelled with its part.

Per benchmark:
1. Yosys synth_xilinx of the exact design (DSP inference on, or off with --dsp off; --dsp both runs the suite twice).
2. (* axc_relax *)/(* axc_precise *) annotation and safe Cone extraction.
3. pyALS Candidate Tournament on the Cone; reintegration into the top.
4. Yosys synth_xilinx of the approximate design.
5. Random-stimulus miter (iverilog) + Yosys SAT formal control/WCE proofs;
   fallback to the exact design if the top-level WCE exceeds the bound.
6. Application-level metric (benchmarks/axbench plugins, selected by the axc_* attributes in annotations.py) on
   exact vs approximate RTL; the same attributes give the format and every constraint. Held-out stream reported.
7. Activity-based dynamic power (axfpga/activity_power.py) of both mapped netlists.

Outputs (in --out-dir, default reports/): results.json, results.csv, report.md. With --dsp both: the same in
<out-dir>/dsp_on and <out-dir>/dsp_off (build directories <build-dir>/dsp_on and <build-dir>/dsp_off), plus
<out-dir>/dsp_comparison.md and dsp_comparison.csv.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import math
import multiprocessing as mp
import os
import pickle
import resource
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# AxFPGA native components
from axfpga.certification import Certification, result_tier
from axfpga.estimator import FpgaEstimator
from axfpga.config import axbench_root
from axfpga import cache
from axfpga.pipeline import (CONE_VARIANTS, F_CLK_MHZ, FPGA_DEVICE, FPGA_PART, MAX_ATTEMPTS, MAX_ATTEMPTS_SMALL, ATTEMPT_SMALL_LCS, MAX_PPG_EVALS,  # noqa: F401
                             DesignSpec, fmt as _fmt, run_design)
from axfpga.quality import QualitySpec

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pyals_yosys_suite")

AXBENCH_ROOT = axbench_root() / "circuits"          # AXFPGA_AXBENCH_ROOT overrides (see axfpga/config.py)
AXFPGA_ROOT = Path(__file__).resolve().parent.parent
AXBENCH_PLUGIN_DIR = AXFPGA_ROOT / "benchmarks" / "axbench"   # plugins and annotations.py (attributes of every design)
BENCH_DIR = AXFPGA_ROOT / "benchmarks"
# Phase 4: behavioural ('*') drop-ins for the AxBench array multipliers (same module name, ports and timing).
BEHAVIOURAL_MULT = {
    "Multiplier_15_0_1000.v": BENCH_DIR / "behavioural" / "Multiplier_15_0_1000_bhv.v",
    "Multiplier_31_0_3000.v": BENCH_DIR / "behavioural" / "Multiplier_31_0_3000_bhv.v",
}
GROUPS = ("axbench", "phase4")
BUILD_DIR = AXFPGA_ROOT / "build" / "pyals_yosys_run"
REPORTS_DIR = AXFPGA_ROOT / "reports"
INNER_JOBS = 3                 # concurrent candidate evaluations inside one benchmark (--inner-jobs)

# Scheduling hints for --jobs > 1. Relative run time orders the queue (longest first);
# EST_GB is the peak resident memory of one concurrent synthesis (tune from build/*/stats.json);
# BASE_GB is the benchmark process itself (python + pyosys + numpy).
EST_COST = {"sobel_npu": 100, "kinematics_inv": 60, "neural_network": 40, "multiplier_32b": 25, "kinematics_fwd": 15,
            # Phase 4 designs: first estimates, not yet measured (tune from build/*/stats.json after the first run)
            "sobel_npu_bhv": 80, "kinematics_inv_bhv": 40, "fft64_psd": 30, "image_pipeline": 20, "conv2d_3x3": 20,
            "neural_network_bhv": 20, "kinematics_fwd_bhv": 10, "multiplier_32b_bhv": 10}
EST_GB = {"sobel_npu": 10.0, "kinematics_inv": 3.5, "multiplier_32b": 1.0, "kinematics_fwd": 2.0, "neural_network": 1.0,  # sobel_npu: 6.7 GB yosys, 9.3 GB iverilog miter
          # Phase 4 designs: first estimates, not yet measured
          "sobel_npu_bhv": 6.0, "kinematics_inv_bhv": 3.0, "kinematics_fwd_bhv": 2.0, "fft64_psd": 1.5,
          "image_pipeline": 1.0, "conv2d_3x3": 1.0, "neural_network_bhv": 1.0, "multiplier_32b_bhv": 0.6}
DEFAULT_GB = 0.4
BASE_GB = 1.4
RESERVE_GB = 2.0               # never plan to use the last RESERVE_GB of available memory
HOLD_S = 120                   # cooldown after a watchdog kill
KILL_BELOW_GB = 1.2            # watchdog: below this much available memory the newest benchmark is stopped and requeued



def _raw_benchmark_suite() -> List[Dict[str, Any]]:
    """The 13 AxBench designs followed by the Phase-4 designs (see _phase4_suite)."""
    axbench = _axbench_suite()
    for b in axbench:
        b["group"] = "axbench"
    return axbench + _phase4_suite(axbench)


def _axbench_suite() -> List[Dict[str, Any]]:
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
            "device": "xc7z020",      # 89 16x16 multipliers: over the xc7z010 in LUTs (DSP off) and DSP48s (DSP on)
        },
    ]


def _behavioural_variant(b: Dict[str, Any]) -> Dict[str, Any]:
    """The design with its AxBench array multiplier replaced by the behavioural one (id suffix _bhv)."""
    files = [BEHAVIOURAL_MULT.get(f.name, f) for f in b["files"]]
    if files == list(b["files"]):
        raise ValueError(f"{b['id']} instantiates no AxBench array multiplier")
    return dict(b, id=f"{b['id']}_bhv", name=f"{b['name']} (behavioural *)", files=files, group="phase4")


def _phase4_suite(axbench: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Phase 4: behavioural-multiplier variants (DSP inference becomes possible) and four larger designs.

    The larger designs are written for this repository (benchmarks/<design>/: RTL + plugin, no third-party code).
    Their quality targets (annotations.py) are provisional until the first full run.
    """
    by_id = {b["id"]: b for b in axbench}
    ann = _annotations_module()
    suite = [_behavioural_variant(by_id[i]) for i in ann.BEHAVIOURAL_VARIANTS]
    new = [
        ("conv2d_3x3", "CNN Conv Layer 3x3x3", "Deep Learning", "conv2d", "conv2d_3x3", "act_out"),
        ("fft64_psd", "FFT-64 Power Spectrum", "Signal Processing", "fft64", "fft64_psd", "out_pow"),
        ("image_pipeline", "Image Edge Pipeline", "Computer Vision", "image_pipeline", "image_pipeline", "edge_out"),
        ("iir_biquad", "IIR Biquad Low-Pass", "DSP Filtering", "iir", "iir_biquad", "y_out"),
    ]
    for bid, name, domain, d, top, wire in new:
        suite.append({
            "id": bid,
            "name": name,
            "domain": domain,
            "top": top,
            "files": [BENCH_DIR / d / f"{top}.v"],
            "relax_wire": wire,
            "precise_signals": ["clk", "rst", "in_valid"],
            "bound": 0.0,                       # replaced by the wce<= constraint of annotations.py
            "metric": "WCE",
            "plugin_dirs": [BENCH_DIR / d],
            "group": "phase4",
        })
    return suite



# Fixes for defects in the AxBench sources: (file name, old text, new text), applied to private copies.
# Benchmarks left out of a default run (still runnable with -b <id>), with the reason shown in the report.
EXCLUDED: Dict[str, str] = {
    "sobel_npu": "out of memory on the 15 GB evaluation machine (Yosys synthesis ~6.7 GB, iverilog miter >9 GB "
                 "for the 42k-LUT patched design); revisited in Phase 3/4",
}

RTL_PATCHES: Dict[str, List[Tuple[str, str, str]]] = {
    # sobel.nn perceptron_fixed instantiates qmult without clk/rst: every multiplier register is unclocked
    # and the design synthesizes to a constant. Wire clk through; tie the active-high rst low.
    "sobel_npu": [
        ("perceptron_fixed.v", "mul_tmp[bitwidth-1:0], over[0]);", "mul_tmp[bitwidth-1:0], over[0], clk, 1'b0);"),
        ("perceptron_fixed.v", "mul_tmp[bitwidth*i+bitwidth-1 -: bitwidth],over[i]);", "mul_tmp[bitwidth*i+bitwidth-1 -: bitwidth],over[i], clk, 1'b0);"),
    ],
}
RTL_PATCHES["sobel_npu_bhv"] = RTL_PATCHES["sobel_npu"]     # same perceptron_fixed.v


def _annotations_module() -> Any:
    spec = importlib.util.spec_from_file_location("axbench_annotations", AXBENCH_PLUGIN_DIR / "annotations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _annotations() -> Dict[str, Dict[str, Any]]:
    """benchmarks/axbench/annotations.py: format, quality constraints, plugin and seed of every design (plain data)."""
    return _annotations_module().ANNOTATIONS


def define_benchmark_suite() -> List[Dict[str, Any]]:
    """Every suite design (AxBench and Phase 4) with its quality attributes applied.

    Budgets, number format, application metric and stimulus all come from annotations.py: the pipeline sees them as
    axc_* attributes on the relaxed wire, exactly as for a user design.
    """
    suite = _raw_benchmark_suite()
    ann = _annotations()
    for b in suite:
        b["quality_attrs"] = ann[b["id"]]
        b["bound"] = QualitySpec.parse(ann[b["id"]]["quality"], b["relax_wire"]).wce_bound
        b["search_dirs"] = [AXBENCH_PLUGIN_DIR, *b.pop("plugin_dirs", [])]
        b["patches"] = RTL_PATCHES.get(b["id"], [])
    return suite


def run_benchmark(bench: Dict[str, Any], estimator: FpgaEstimator, inner_jobs: int = 1) -> Dict[str, Any]:
    """Run one benchmark through the AxFPGA pipeline in BUILD_DIR/<id>."""
    return run_design(DesignSpec.from_dict(bench), estimator, inner_jobs, work_dir=BUILD_DIR / bench["id"],
                      evaluator=None, cone_variants=CONE_VARIANTS)




def _json_default(o: Any) -> Any:
    return None if isinstance(o, float) and math.isinf(o) else str(o)


def merge_results(build_dir: Path, out_dir: Path) -> List[Dict[str, Any]]:
    """Merge the per-benchmark ``<build_dir>/<id>/result.json`` files into the reports in ``out_dir``."""
    results = []
    for b in define_benchmark_suite():
        rj = build_dir / b["id"] / "result.json"
        if rj.exists():
            results.append(json.loads(rj.read_text()))
    if results:
        export_results(results, out_dir)
    return results


def _rss_gb(pgids: Set[int]) -> float:
    """Resident memory (GB) of every process in the given process groups (the benchmarks and all their children)."""
    total_kb = 0
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
            pgrp = int(stat[stat.rindex(")") + 2:].split()[2])
            if pgrp in pgids:
                with open(f"/proc/{entry}/statm") as f:
                    total_kb += int(f.read().split()[1]) * page_kb
        except (OSError, ValueError, IndexError):
            continue
    return total_kb / 1048576.0


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
    headers += ["certification", "wce", "wce_pct", "out_bits", "med", "mred_pct", "er", "formal_control", "formal_wce", "app_metric", "app_value", "app_unit", "app_threshold", "app_pass",
                "heldout_wce", "heldout_med", "heldout_app_value", "heldout_ok", "device", "dsp_inference"]
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in results:
            row = [r["id"], r["name"], r["method"], r["fell_back"], r["bound"]]
            for k in res_keys:
                row += [r["exact"][k], r["approx"][k], r["savings_pct"][k]]
            e, a = r["errors"], r["app"]
            row += [result_tier(r).value, e["wce"], e.get("wce_pct"), e.get("out_bits"), e["med"], e["mred_pct"], e["er"], r["formal"]["control_equivalence"], r["formal"]["wce_bound"],
                    a["name"], _fmt(a["value"], 4), a["unit"], a["threshold"], a["pass"]]
            h = r.get("heldout")
            row += [h["errors"]["wce"], h["errors"]["med"], _fmt(h["app"]["value"], 4), h["within_limits"]] if h else ["", "", "", ""]
            row += [r["device"], r.get("dsp_inference", "")]
            w.writerow(row)

    # Only certified results (every applicable proof PROVEN) feed the headline tables; the rest are listed below them.
    all_results = results
    results = [r for r in all_results if result_tier(r) is Certification.CERTIFIED]
    uncertified = [r for r in all_results if result_tier(r) is not Certification.CERTIFIED]
    pm = all_results[0]["power_model"] if all_results else {}
    dsp_on = all(r.get("dsp_inference", True) for r in all_results)
    dsp_text = "DSP inference allowed" if dsp_on else "DSP inference off: `-nodsp`"
    big = [r["name"] for r in all_results if r["device"] != FPGA_PART]
    md: List[str] = [
        "# AxFPGA AxBench Evaluation: Zybo Z7-10 (xc7z010clg400-1)",
        "",
        f"Synthesis: Yosys `synth_xilinx -family xc7 -flatten -noiopad{'' if dsp_on else ' -nodsp'}` ({dsp_text}). "
        "Approximation: pyALS Candidate Tournament. LC = LUT + 2 x CARRY4. "
        "Utilization % is relative to the xc7z010 (17,600 LUT, 35,200 FF, 80 DSP48E1, 60 BRAM36)"
        + (f"; designs marked [xc7z020] are larger than the xc7z010 and relative to the xc7z020 "
           f"(53,200 LUT, 106,400 FF, 220 DSP48E1, 140 BRAM36): {', '.join(big)}." if big else "."),
        "",
    ]
    excluded = [b for b in define_benchmark_suite() if b["id"] in EXCLUDED and b["id"] not in {r["id"] for r in all_results}]
    if excluded:
        md += ["Excluded from this run: " + "; ".join(f"**{b['name']}** ({EXCLUDED[b['id']]})" for b in excluded) + ".", ""]
    md += [
        "## 1. Utilization: baseline vs approximate",
        "",
        "| Benchmark | Champion | LUT | FF | DSP | BRAM | CARRY4 | LC | LC saved |",
        "|:--|:--|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for r in results:
        e, a, s = r["exact"], r["approx"], r["savings_pct"]
        cell = lambda k: f"{e[k]} → {a[k]}"
        label = r["name"] + (" [xc7z020]" if r["device"] != FPGA_PART else "")
        md.append(f"| **{label}** | `{r['method']}` | {cell('luts')} | {cell('ffs')} | {cell('dsps')} | {cell('brams')} | {cell('carry4')} | {cell('logic_cells')} | **{s['logic_cells']:.1f}%** |")
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
           "(two's complement / sign-magnitude, LSB units). Limits are the axc_quality constraints of each design (benchmarks/axbench/annotations.py). "
           "Every reported approximation satisfies all of them (typically WCE, MED, MRED and the application threshold), "
           "intact control outputs, and no FALSIFIED formal proof. "
           "Designs with array multipliers first try design-level partial-product pruning (largest in-budget anti-diagonal depth, "
           f"binary search, {MAX_PPG_EVALS} steps max); otherwise cone candidates are tried cheapest first (up to {MAX_ATTEMPTS}, or {MAX_ATTEMPTS_SMALL} for designs of at most {ATTEMPT_SMALL_LCS} LC, each "
           f"reintegrated as {' then '.join(CONE_VARIANTS)}). If none passes, the exact design is reported.", "",
           "| Benchmark | WCE (limit) | WCE % of full scale | WCE % of limit | MED (limit) | MRED % (limit) | ER | Formal control | Formal WCE |",
           "|:--|--:|--:|--:|--:|--:|--:|:--|:--|"]
    for r in results:
        e, f, lim = r["errors"], r["formal"], r["limits"]
        wp = e.get("wce_pct")
        wl = 100.0 * e["wce"] / lim["wce"] if lim.get("wce") else None
        md.append(f"| {r['name']} | {e['wce']:.0f} ({lim['wce']:g}) | {'n/a' if wp is None else format(wp, '.6g') + ' %'} | "
                  f"{'n/a' if wl is None else format(wl, '.2f') + ' %'} | {e['med']:.3f} ({lim['med']:g}) | {e['mred_pct']:.4f} ({'n/a' if lim['mred_pct'] is None else format(lim['mred_pct'], 'g')}) | "
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

    md += ["", "### Held-out stream", "",
           "Selection used the stimulus above only. The accepted design is re-evaluated once on an independent stream "
           "(derived seed, other image, or second half of a stimulus file); reported, not enforced.", "",
           "| Benchmark | WCE | MED | Application metric | Within limits | Stimulus |", "|:--|--:|--:|:--|:--|:--|"]
    for r in results:
        h = r.get("heldout")
        if not h:
            md.append(f"| {r['name']} | n/a | n/a | n/a | n/a | {r.get('heldout_reason') or ''} |")
            continue
        a = h["app"]
        md.append(f"| {r['name']} | {h['errors']['wce']:.0f} | {h['errors']['med']:.3f} | {a['name']} {_fmt(a['value'], 3)} {a['unit']} | "
                  f"{'yes' if h['within_limits'] else 'NO'} | {h['note']} |")

    if uncertified:
        md += ["", "## Not certified (excluded from the tables above)", "",
               "| Benchmark | Tier | Reason |", "|:--|:--|:--|"]
        for r in uncertified:
            cert = r.get("certification") or {}
            reason = cert.get("reason") or "formal: " + ", ".join(f"{k}={v}" for k, v in r["formal"].items())
            md.append(f"| {r['name']} | {result_tier(r).value} | {reason} |")

    md += ["", "## 5. Candidate attempts", ""]
    for r in all_results:
        head = f"exact design kept: {r['fallback_reason']}" if r["fell_back"] else f"accepted `{r['method']}`"
        md.append(f"- **{r['name']}**: {head}")
        for a in r.get("attempts", []):
            why = "accepted" if a.get("outcome") == "accepted" else a.get("reason", "")
            md.append(f"  - `{a['candidate']}` [{a.get('variant', 'remapped')}]: {why}")
    md_path.write_text("\n".join(md) + "\n")
    logger.info("Wrote %s, %s, %s", json_path, csv_path, md_path)


def export_dsp_comparison(on: List[Dict[str, Any]], off: List[Dict[str, Any]], out_dir: Path) -> List[str]:
    """dsp_comparison.md / .csv: every design with DSP inference on and off, side by side (Phase 4).

    Returns the gate failures: designs missing in one of the runs, and behavioural-multiplier variants (id ending in
    _bhv) whose exact design infers no DSP48E1 with DSP inference on.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    on_by, off_by = {r["id"]: r for r in on}, {r["id"]: r for r in off}
    order = [b["id"] for b in define_benchmark_suite()]
    ids = sorted(set(on_by) | set(off_by), key=lambda i: order.index(i) if i in order else 99)
    failures: List[str] = []
    rows: List[List[Any]] = []
    md = ["# DSP inference on vs off", "",
          "Same designs, same flow; only `synth_xilinx -nodsp` differs. LC = LUT + 2 x CARRY4. "
          "DSP = DSP48E1 count. Cells: exact -> approximate.", "",
          "| Benchmark | DSP (on) | LUT (on) | LC (on) | LC saved (on) | Tier (on) | LUT (off) | LC (off) | LC saved (off) | Tier (off) |",
          "|:--|--:|--:|--:|--:|:--|--:|--:|--:|:--|"]

    def part(r: Optional[Dict[str, Any]]) -> Tuple[List[Any], List[str]]:
        if r is None:
            return [None] * 7, ["missing"] * 4
        e, a, s = r["exact"], r["approx"], r["savings_pct"]
        vals = [e["dsps"], a["dsps"], e["luts"], a["luts"], e["logic_cells"], a["logic_cells"], s["logic_cells"]]
        cells = [f"{e['luts']} -> {a['luts']}", f"{e['logic_cells']} -> {a['logic_cells']}", f"{s['logic_cells']:.1f}%",
                 result_tier(r).value]
        return vals, cells

    for i in ids:
        r_on, r_off = on_by.get(i), off_by.get(i)
        name = (r_on or r_off)["name"]
        v_on, c_on = part(r_on)
        v_off, c_off = part(r_off)
        dsp_cell = "missing" if r_on is None else f"{v_on[0]} -> {v_on[1]}"
        md.append(f"| {name} | {dsp_cell} | {' | '.join(c_on)} | {' | '.join(c_off[:3])} | {c_off[3]} |")
        rows.append([i, name, *v_on, *v_off[2:]])
        if r_on is None or r_off is None:
            failures.append(f"{i}: no result with DSP inference {'on' if r_on is None else 'off'}")
        elif i.endswith("_bhv") and r_on["exact"]["dsps"] == 0:
            failures.append(f"{i}: behavioural variant infers no DSP48E1 with DSP inference on")
        if r_off is not None and r_off["exact"]["dsps"] != 0:
            failures.append(f"{i}: {r_off['exact']['dsps']} DSP48E1 with DSP inference off")
    md += ["", "## Gate checks", ""]
    md += [f"- FAIL: {f}" for f in failures] or ["- PASS: every design has both runs; every `_bhv` variant infers "
                                                 "DSP48E1 blocks with DSP inference on; no design uses DSP48E1 with "
                                                 "DSP inference off."]
    (out_dir / "dsp_comparison.md").write_text("\n".join(md) + "\n")
    with open(out_dir / "dsp_comparison.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "on_exact_dsps", "on_approx_dsps", "on_exact_luts", "on_approx_luts", "on_exact_lc",
                    "on_approx_lc", "on_lc_saved_pct", "off_exact_luts", "off_approx_luts", "off_exact_lc",
                    "off_approx_lc", "off_lc_saved_pct"])
        w.writerows(rows)
    logger.info("Wrote %s (%d gate failure(s))", out_dir / "dsp_comparison.md", len(failures))
    return failures


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


def _inner_jobs_for(bench_id: str, inner_jobs: int, available_gb: float) -> int:
    """Concurrent candidate evaluations for one benchmark, from the memory still unplanned."""
    gb = EST_GB.get(bench_id, DEFAULT_GB)
    return max(1, min(inner_jobs, int((available_gb - BASE_GB) // gb)))


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
    cache.reset_stats()
    result = run_benchmark(bench, FpgaEstimator(family="xc7", device=bench.get("device", FPGA_DEVICE), nodsp=nodsp),
                           inner_jobs)
    logger.info("%s", cache.summary())
    stats = {"id": bench_id, "wall_s": round(time.time() - t0, 1), "inner_jobs": inner_jobs, "cache": cache.stats(),
             "peak_child_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, 1),
             "peak_self_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)}
    (BUILD_DIR / bench_id / "stats.json").write_text(json.dumps(stats, indent=2))
    (BUILD_DIR / bench_id / "result.json").write_text(json.dumps(result, indent=2, default=_json_default))
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
    peak_rss = 0.0                      # peak resident memory (GB) of all benchmark processes and their children

    def reap(bench_id: str, proc: Any, rpath: Path, t_start: float) -> None:
        proc.join()
        dt = time.time() - t_start
        if proc.exitcode == 0 and rpath.exists():
            logger.info("[scheduler] %s done in %.0f s (result: %s)", bench_id, dt, BUILD_DIR / bench_id / "result.json")
        else:
            logger.error("[scheduler] %s FAILED (exit code %s) after %.0f s", bench_id, proc.exitcode, dt)
            failed.append(bench_id)

    while pending or running:
        for b in list(pending):
            if len(running) >= jobs or (running and time.time() < hold_until):
                break
            planned = sum(v[1] for v in running.values())
            ij = min(_inner_jobs_for(b["id"], inner_jobs, budget_gb - planned), forced_inner.get(b["id"], inner_jobs))
            gb = BASE_GB + EST_GB.get(b["id"], DEFAULT_GB) * ij
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
        peak_rss = max(peak_rss, _rss_gb({v[0].pid for v in running.values()}))
        time.sleep(2)
    run_suite.peak_rss_gb = round(peak_rss, 2)
    logger.info("[scheduler] peak resident memory of all benchmark processes: %.2f GB", peak_rss)
    return failed


def _dsp_runs(mode: str, out_dir: Path, build_dir: Path) -> List[Tuple[bool, Path, Path]]:
    """(nodsp, out dir, build dir) per suite run: one run for --dsp on/off, two subdirectories for --dsp both."""
    if mode == "both":
        return [(False, out_dir / "dsp_on", build_dir / "dsp_on"), (True, out_dir / "dsp_off", build_dir / "dsp_off")]
    return [(mode == "off", out_dir, build_dir)]


def select_suite(benchmarks: Optional[List[str]], group: str) -> List[Dict[str, Any]]:
    """Designs of one run: the -b ids (or tops) if given, else the group without the EXCLUDED designs."""
    suite = define_benchmark_suite()
    if benchmarks:
        return [b for b in suite if b["id"] in benchmarks or b["top"] in benchmarks]
    if group != "all":
        suite = [b for b in suite if b["group"] == group]
    for b in suite:
        if b["id"] in EXCLUDED:
            logger.warning("Skipping %s: %s (run it with -b %s)", b["id"], EXCLUDED[b["id"]], b["id"])
    return [b for b in suite if b["id"] not in EXCLUDED]


def main():
    global BUILD_DIR, CONE_VARIANTS
    parser = argparse.ArgumentParser(description="Benchmark-suite evaluation of AxFPGA on Zybo Z7-10 (Yosys only)")
    parser.add_argument("--benchmark", "-b", action="append", default=None, help="Benchmark id (repeatable); default all")
    parser.add_argument("--group", choices=("all", *GROUPS), default="all",
                        help="Designs run when no -b is given: the AxBench designs, the Phase-4 designs, or all (default)")
    parser.add_argument("--out-dir", "-o", default=str(REPORTS_DIR), help="Directory for results.json/csv and report.md")
    parser.add_argument("--build-dir", default=str(BUILD_DIR), help="Working directory for per-benchmark artifacts")
    parser.add_argument("--dsp", choices=("on", "off", "both"), default="on",
                        help="DSP48E1 inference: on (default), off (-nodsp), or both (two runs in dsp_on/ and dsp_off/ "
                             "subdirectories of --out-dir and --build-dir, plus dsp_comparison.md)")
    parser.add_argument("--nodsp", action="store_true", help="Alias for --dsp off")
    parser.add_argument("--cone-variants", default=",".join(CONE_VARIANTS),
                        help="Comma-separated reintegration variants tried per candidate, in order: raw, remapped")
    parser.add_argument("--jobs", "-j", type=int, default=1,
                        help="Benchmarks run at once, each in its own process (default 1 = serial, in-process)")
    parser.add_argument("--inner-jobs", type=int, default=INNER_JOBS,
                        help="Concurrent candidate evaluations inside one benchmark (1 = serial); results are identical")
    parser.add_argument("--no-cache", action="store_true", help="Do not read or write the content-hash cache (build/.cache)")
    parser.add_argument("--merge", action="store_true",
                        help="Only merge the per-benchmark result.json files of --build-dir into the reports of --out-dir")
    parser.add_argument("--mem-gb", type=float, default=None,
                        help="Memory budget for the scheduler in GB (default 70%% of RAM)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir).resolve()
    build_root = Path(args.build_dir).resolve()
    CONE_VARIANTS = tuple(v.strip() for v in args.cone_variants.split(",") if v.strip() in ("raw", "remapped")) or ("remapped",)
    budget_gb = args.mem_gb if args.mem_gb else _mem_total_gb() * 0.7
    dsp_mode = "off" if args.nodsp else args.dsp
    runs = _dsp_runs(dsp_mode, out_dir, build_root)

    if args.no_cache:
        cache.disable()                 # inherited by the benchmark processes
    if args.merge:
        merged_by_mode = []
        for _, run_out, run_build in runs:
            merged = merge_results(run_build, run_out)
            merged_by_mode.append(merged)
            logger.info("Merged %d result(s) from %s into %s", len(merged), run_build, run_out)
        if dsp_mode == "both":
            export_dsp_comparison(merged_by_mode[0], merged_by_mode[1], out_dir)
        return
    suite = select_suite(args.benchmark, args.group)
    if not suite:
        logger.error("No benchmark matches %s", args.benchmark)
        sys.exit(2)

    t_all = time.time()
    failed_any: List[str] = []
    results_by_mode: List[List[Dict[str, Any]]] = []
    for nodsp, run_out, run_build in runs:
        BUILD_DIR = run_build
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        logger.info("Suite run: %d design(s), DSP inference %s, build %s", len(suite), "off" if nodsp else "on", BUILD_DIR)
        # Every benchmark runs in its own process, also with --jobs 1: libyosys keeps global state (name counters), so
        # benchmarks sharing a process would give results that depend on the run order and on --jobs.
        failed = run_suite(suite, run_out, max(1, args.jobs), max(1, args.inner_jobs), budget_gb, nodsp)
        failed_any += [f"{i} (dsp {'off' if nodsp else 'on'})" for i in failed]
        ids = {b["id"] for b in suite} - set(failed)
        results = [json.loads((BUILD_DIR / i / "result.json").read_text()) for i in sorted(ids) if (BUILD_DIR / i / "result.json").exists()]
        results_by_mode.append(results)
        if results:
            export_results(results, run_out)
        wall = time.time() - t0
        hits = misses = 0
        for i in ids:
            sp = BUILD_DIR / i / "stats.json"
            if sp.exists():
                for v in json.loads(sp.read_text()).get("cache", {}).values():
                    hits += v["hits"]
                    misses += v["misses"]
        run_out.mkdir(parents=True, exist_ok=True)
        (run_out / "run_stats.json").write_text(json.dumps({
            "wall_s": round(wall, 1), "jobs": args.jobs, "inner_jobs": args.inner_jobs, "benchmarks": sorted(ids),
            "dsp_inference": not nodsp,
            "peak_rss_gb": getattr(run_suite, "peak_rss_gb", None), "cache_enabled": cache.enabled(),
            "cache_hits": hits, "cache_misses": misses}, indent=2))
        logger.info("Done in %.1f s (DSP inference %s). Failed: %s. Cache: %d hit / %d miss",
                    wall, "off" if nodsp else "on", failed or "none", hits, misses)
    if dsp_mode == "both":
        export_dsp_comparison(results_by_mode[0], results_by_mode[1], out_dir)
        logger.info("Both DSP runs done in %.1f s", time.time() - t_all)
    sys.exit(1 if failed_any else 0)


if __name__ == "__main__":
    main()
