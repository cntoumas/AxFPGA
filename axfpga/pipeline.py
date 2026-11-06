"""AxFPGA pipeline: one design through Yosys synthesis, PPG pruning / pyALS Tournament, verification, power.

Public API
    DesignSpec      what to approximate (files, top, relaxed output, precise inputs, error budget)
    run_design      full flow on one spec -> result dict (exact vs approximate utilization, errors, formal, power)
    GenericEvaluator   seeded random stimulus + error statistics for any design (used by the axfpga CLI)
    annotate_verilog, fmt
    _top_ports, _normalize_rtl, _reintegrate, _formal_control, _formal_checks   (building blocks)

Application-level checks (stimulus, error statistics, application metric, power vectors) are supplied by an
``evaluator`` object, so the library does not depend on the benchmark scripts. It must provide
    build_stimulus(id) -> (vectors, ctx)
    error_stats(id, exact_outputs, approx_outputs, relax_wire, skip) -> {wce, med, mred, er}
    app_metrics(id, vectors, exact_outputs, approx_outputs, relax_wire=, skip=) -> dict
    power_vectors(id, vectors) -> vectors
and, optionally, for the held-out stream
    build_heldout(id) -> (vectors, ctx) | None
    app_metrics_for(stream, vectors, exact_outputs, approx_outputs, skip) -> dict
Pass ``evaluator=None`` to let the pipeline build a ``QualityEvaluator`` from the axc_* attributes of the relaxed wire
(axfpga/quality): axc_format, axc_quality constraints, axc_stimulus / axc_plugin / axc_seed. Selection uses the tuning
stream; the held-out stream is evaluated on the accepted design and reported in ``result["heldout"]``. For PPG-pruned
designs (no WCE proof) it also gates acceptance: a depth that fails a constraint there is skipped.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pyosys import libyosys as ys
from axfpga.activity_power import estimate_dynamic_power, run_vector_sim
from axfpga.cone_extractor import ConeExtractor
from axfpga.certification import certification_record
from axfpga import cache
from axfpga.config import yosys_cmd
from axfpga.estimator import FpgaEstimator
from axfpga.explore.design import DesignEvaluator
from axfpga.explore.evaluate import LOWER_METRICS, ConeEvaluator, error_metrics
from axfpga.explore.stage import algorithm_from_env, cone_techniques, explore, techniques_from_env
from axfpga.explore.techniques import PpgDesignTechnique, RegTechnique
from axfpga.operators import OperatorGenerator
from axfpga.operators.design_level import run_design_level
from axfpga.parallel import Cancelled, depth_search, first_accepted, yosys_serialized
from axfpga.ppg_pruner import PpgPruner
from axfpga.ports import resolve_clock_reset
from axfpga.pyals_bridge import PyAlsBridge
from axfpga.quality import QualitySpec, make_evaluator, power_cap
from axfpga.quality.attrs import WireQuality
from axfpga.reintegrator import Reintegrator
from axfpga.tournament import DSP_LC_WEIGHT, Tournament
from axfpga.verifier import FormalControlVerifier, FormalWceVerifier, MiterVerifier

logger = logging.getLogger("axfpga.pipeline")

FPGA_PART = "xc7z010clg400-1"
FPGA_DEVICE = "xc7z010"
# Parts by device: designs larger than the xc7z010 are evaluated on the xc7z020 and labelled with its part.
FPGA_PARTS = {"xc7z010": FPGA_PART, "xc7z020": "xc7z020clg400-1"}
F_CLK_MHZ = 125.0
# Cycles of the activity-power simulation (evenly spaced from the power stimulus). Kept short since Phase 4: the
# gate-level VCD simulation of large designs took tens of minutes, and Vivado SAIF power (Phase 9) is the reference.
POWER_CYCLES = 256
TOURNAMENT_SAMPLES = 256        # random cone vectors per candidate in the Tournament (64 until Phase 5)
FORMAL_TIMEOUT_S = 120
SIM_TIMEOUT_S = 3600           # miter compile/run timeout (large designs, e.g. sobel_npu at 42k LUTs)
REG_KS = (1, 2, 3, 4, 6, 8)    # operand-truncation depths tried behind registers (Stage 3c), with corrections zero and mid
MAX_ATTEMPTS = 8               # candidates tried per design before keeping the exact design
MAX_ATTEMPTS_SMALL = 24        # ... for designs of at most ATTEMPT_SMALL_LCS logic cells: an attempt costs seconds there, and
ATTEMPT_SMALL_LCS = 1500       # the operator stacks fill a window of 8 with variants of one aggressive genome (Phase 5)
MED_FRACTION_OF_BOUND = 0.5    # MED limit = WCE bound / 2
MRED_LIMIT = 0.01              # MRED limit = 1 %
MAX_PPG_EVALS = 7              # binary-search steps over the PPG anti-diagonal depth
CONE_VARIANTS = ("raw", "remapped")  # reintegration variants tried per candidate, in order


@dataclass
class DesignSpec:
    """One design to approximate. ``bound`` is the WCE budget in LSB units of the relaxed output."""
    id: str
    top: str
    files: List[Path]
    relax_wire: str
    bound: float
    name: str = ""
    domain: str = ""
    precise_signals: List[str] = field(default_factory=list)
    metric: str = "WCE"
    med_limit: Optional[float] = None       # None disables; from_dict defaults it to bound * MED_FRACTION_OF_BOUND
    mred_limit: Optional[float] = None      # None disables; from_dict defaults it to MRED_LIMIT
    patches: List[Tuple[str, str, str]] = field(default_factory=list)   # (file name, old text, new text) on private copies
    quality_attrs: Dict[str, Any] = field(default_factory=dict)   # axc_* attributes inserted on the relaxed wire of the
                              # private annotated copy: {"format": "sm16q9", "quality": "wce<=26;...", "plugin": "x.py", "seed": 1}
    search_dirs: List[Path] = field(default_factory=list)          # where relative plugin / stimulus paths are looked up
    stimulus: Optional[str] = None          # overrides axc_stimulus
    plugin: Optional[str] = None            # overrides axc_plugin
    seed: Optional[int] = None              # overrides axc_seed
    samples: int = 1000                     # default random stimulus length (axc_samples overrides)
    signed: bool = False                    # Verilog-signed relaxed wire (selects the legacy MRED reference)
    clock: Optional[str] = None             # explicit clock / reset ports (axc_clock / axc_reset attributes win)
    reset: Optional[str] = None
    reset_active_low: Optional[bool] = None
    through_registers: bool = True    # also relax and approximate the arithmetic behind internal registers (Stage 3c);
                              # the environment variable AXFPGA_THROUGH_REGISTERS=0 turns it off for a whole run
    annotated: bool = False   # sources already carry (* axc_relax *) / (* axc_precise *); do not insert attributes.
                              # bound/metric then override the attribute values in the Cone metadata.

    def __post_init__(self) -> None:
        self.files = [Path(f) for f in self.files]
        self.search_dirs = [Path(d) for d in self.search_dirs]
        self.name = self.name or self.id

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DesignSpec":
        """Spec from a benchmark-style dict; unknown keys are ignored, MED/MRED limits default when absent."""
        names = {f.name for f in dataclasses.fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        kw["med_limit"] = d["med_limit"] if "med_limit" in d else float(d["bound"]) * MED_FRACTION_OF_BOUND
        kw["mred_limit"] = d["mred_limit"] if "mred_limit" in d else MRED_LIMIT
        return cls(**kw)


def annotate_verilog(
    input_files: List[Path],
    output_dir: Path,
    top_module: str,
    relax_wire: str,
    precise_signals: List[str],
    bound: float,
    metric: str = "wce",
    attrs: Optional[Dict[str, Any]] = None,
    clock: Optional[str] = None,
    reset: Optional[str] = None,
    reset_active_low: Optional[bool] = None,
) -> List[Path]:
    """Insert IEEE Verilog attributes (* axc_relax *) and (* axc_precise *) into design files.

    ``attrs`` adds more axc_* items to the relaxed wire ("format" or "axc_format" -> axc_format="..."); ``clock`` /
    ``reset`` add the axc_clock / axc_reset markers to those input ports.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    annotated_files: List[Path] = []

    extra = ""
    for k, v in (attrs or {}).items():
        k = k if k.startswith("axc_") else f"axc_{k}"
        extra += f', {k}={v}' if isinstance(v, (int, float)) and not isinstance(v, bool) else f', {k}="{v}"'
    attr_relax = f'(* axc_relax, axc_metric="{metric.lower()}", axc_bound={int(bound)}{extra} *) '

    def port_attr(name: str) -> str:
        roles = []
        if name in precise_signals:
            roles.append("axc_precise")
        if name == clock:
            roles.append("axc_clock")
        if name == reset:
            roles.append('axc_reset="low"' if reset_active_low else "axc_reset")
        return f"(* {', '.join(roles)} *) "

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

            for p_sig in list(dict.fromkeys([*precise_signals, *(n for n in (clock, reset) if n)])):
                attr_p = port_attr(p_sig)
                pat_in = rf"(?m)^([ \t]*)input\s+((?:\[[^\]]+\]\s*)?{re.escape(p_sig)}\b)"
                if re.search(pat_in, new_top):
                    new_top = re.sub(pat_in, lambda m: f"{m.group(1)}{attr_p}input {m.group(2)}", new_top, count=1)
                else:
                    pat_in_any = rf"\binput\s+((?:\[[^\]]+\]\s*)?{re.escape(p_sig)}\b)"
                    new_top = re.sub(pat_in_any, lambda m: f"{attr_p}input {m.group(1)}", new_top, count=1)

            modified = modified[:top_m.start()] + new_top + modified[top_m.end():]

        out_path = output_dir / fpath.name
        out_path.write_text(modified, encoding="utf-8")
        annotated_files.append(out_path)

    return annotated_files


def _patched_files(spec: DesignSpec, work: Path) -> List[Path]:
    """Apply spec.patches to private copies of the sources (originals untouched)."""
    if not spec.patches:
        return list(spec.files)
    out_dir = work / "patched_rtl"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for f in spec.files:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for name, old, new in spec.patches:
            if f.name == name:
                if old not in txt:
                    raise RuntimeError(f"RTL patch for {spec.id} does not apply to {name}: {old!r}")
                txt = txt.replace(old, new)
        dst = out_dir / f.name
        dst.write_text(txt, encoding="utf-8")
        files.append(dst)
    logger.info("Applied %d RTL patch(es) for %s", len(spec.patches), spec.id)
    return files


@yosys_serialized
def _top_ports(files: List[Path], top: str, clock: Optional[str] = None, reset: Optional[str] = None,
               reset_active_low: Optional[bool] = None, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Top-level ports of the exact design, with clock/reset split out.

    Clock and reset: axc_clock / axc_reset attributes (``meta``), then the explicit arguments, then a name guess.
    """
    d = ys.Design()
    ys.run_pass(" ".join(f"read_verilog -sv {f};" for f in files) + f" hierarchy -top {top}", d)
    mod = d.top_module()
    ins = [(w.name.str().lstrip("\\"), w.width) for w in mod.wires_.values() if w.port_input]
    outs = [(w.name.str().lstrip("\\"), w.width) for w in mod.wires_.values() if w.port_output]
    cr = resolve_clock_reset([{"name": n, "width": w} for n, w in ins], meta or {}, clock, reset, reset_active_low)
    clk, rst = cr["clk"], cr["rst"]
    data_ins = [(n, w) for n, w in ins if n not in (clk, rst)]
    return {"inputs": data_ins, "outputs": outs, "clk": clk, "rst": rst,
            "rst_active_low": bool(rst and cr["rst_active_low"]), "all_inputs": ins}


def _with_meta_ports(ports: Dict[str, Any], meta: Dict[str, Any], clock: Optional[str], reset: Optional[str],
                     reset_active_low: Optional[bool]) -> Dict[str, Any]:
    """Re-resolve clock / reset once the cone metadata (axc_clock / axc_reset) is known. Pure Python: the Yosys
    read of the exact design stays where it was (before the cone extraction), as IdString order depends on it."""
    if not (meta.get("clock") or meta.get("reset")):
        return ports
    ins = ports["all_inputs"]
    cr = resolve_clock_reset([{"name": n, "width": w} for n, w in ins], meta, clock, reset, reset_active_low)
    clk, rst = cr["clk"], cr["rst"]
    return {**ports, "inputs": [(n, w) for n, w in ins if n not in (clk, rst)], "clk": clk, "rst": rst,
            "rst_active_low": bool(rst and cr["rst_active_low"])}


def _normalize_rtl(src: Path, top: str, out: Path) -> Path:
    """Yosys-normalized netlist (proc; flatten) with synthesis semantics.

    Some AxBench sources use blocking assignments in chained flip-flops (e.g. fir/dff),
    which race in event-driven RTL simulation; Yosys gives the hardware semantics.
    """
    lib = "read_verilog -lib +/xilinx/cells_sim.v; " if any(
        p in src.read_text(errors="ignore") for p in ("LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "CARRY4", "FDRE")) else ""
    ckey = cache.make_key("norm", [src], top=top, lib=bool(lib))
    hit = cache.load("norm", ckey)
    if hit is not None and cache.restore(hit[1], "norm.v", out):
        return out
    cmd = (f"{lib}read_verilog -sv {src}; hierarchy -check -top {top}; setattr -unset keep_hierarchy; "
           f"proc; flatten; opt_clean; write_verilog -noattr {out}")
    res = subprocess.run(yosys_cmd() + ["-q", "-p", cmd], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Yosys normalization of {src.name} failed: {res.stderr[-1500:]}")
    cache.save("norm", ckey, True, {"norm.v": out})
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
                   meta_json: Optional[Path], top_mod: str, check_wce: bool = True) -> Tuple[str, str]:
    """Yosys SAT: control-output equivalence of the tops, WCE bound of the cones."""
    try:
        ctrl = FormalControlVerifier(exact_vlog=exact_top_v, approx_vlog=approx_top_v, meta_json=meta_json,
                                     timeout=FORMAL_TIMEOUT_S).verify().get("status", "ERROR")
    except Exception as exc:
        logger.warning("Formal control check error: %s", exc)
        ctrl = "ERROR"
    if not check_wce:
        return ctrl, "N/A (metric is not a worst-case bound)"
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
        "primitives": dict(res.get("cells_by_type") or {}),
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


def run_design(spec: Union[DesignSpec, Dict[str, Any]], estimator: FpgaEstimator, inner_jobs: int = 1, *,
               work_dir: Union[str, Path], evaluator: Any, cone_variants: Sequence[str] = CONE_VARIANTS,
               tournament_estimator: Optional[FpgaEstimator] = None) -> Dict[str, Any]:
    """Run the full Yosys + pyALS flow, verification, application metric and power on one design.

    ``work_dir`` is the design's private directory (deleted and recreated). ``evaluator`` supplies the application
    stimulus and metrics (see the module docstring). ``inner_jobs`` > 1 evaluates candidates concurrently
    (subprocess steps in parallel, in-process pyosys steps serialized); the result is identical to 1.
    ``tournament_estimator`` costs the cone candidates (default: xc7z010 with the DSP setting of ``estimator``; until
    Phase 4 it always allowed DSPs, which made every cone with a multiplier look almost free in a -nodsp run).
    """
    if not isinstance(spec, DesignSpec):
        spec = DesignSpec.from_dict(spec)
    variants = tuple(cone_variants)
    bench = dataclasses.asdict(spec)
    bench["files"] = list(spec.files)
    bench_id = spec.id
    top_mod = spec.top
    part = FPGA_PARTS.get(getattr(estimator, "device", FPGA_DEVICE), FPGA_PART)
    logger.info("=" * 80)
    logger.info("Design: %s (%s) on %s | inner jobs: %d", spec.name, bench_id, part, inner_jobs)
    logger.info("=" * 80)

    bench_work = Path(work_dir)
    if bench_work.exists():
        shutil.rmtree(bench_work)
    bench_work.mkdir(parents=True, exist_ok=True)
    bench = dict(bench, files=_patched_files(spec, bench_work))

    exact_top_v = bench_work / "exact_baseline_top.v"
    exact_top_v.write_text("\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in bench["files"]), encoding="utf-8")

    ports = _top_ports(bench["files"], top_mod, bench["clock"], bench["reset"], bench["reset_active_low"])
    app_dir = bench_work / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: exact baseline synthesis and exact application reference. Both are subprocess-only,
    # so they overlap with the cone extraction (pyosys, main thread) below.
    logger.info("[Stage 1] Yosys synth_xilinx of exact baseline...")
    exact_map_v, exact_map_j = bench_work / "exact_mapped.v", bench_work / "exact_mapped.json"

    pre = ThreadPoolExecutor(max_workers=2 if inner_jobs > 1 else 1)
    f_synth = pre.submit(estimator.evaluate_netlist, exact_top_v, top_module=top_mod, work_dir=bench_work,
                         out_mapped_vlog=exact_map_v, out_mapped_json=exact_map_j)

    # Stage 2: Attribute Annotation & Safe Cone Extraction
    logger.info("[Stage 2] Annotating Verilog attributes and extracting safe combinational cone...")
    annotated_dir = bench_work / "annotated_rtl"
    if bench["annotated"]:
        annotated_dir.mkdir(parents=True, exist_ok=True)
        annotated_files = []
        for f in bench["files"]:
            shutil.copy(f, annotated_dir / f.name)
            annotated_files.append(annotated_dir / f.name)
    else:
        annotated_files = annotate_verilog(
            input_files=bench["files"],
            output_dir=annotated_dir,
            top_module=top_mod,
            relax_wire=bench["relax_wire"],
            precise_signals=bench["precise_signals"],
            bound=bench["bound"],
            metric=bench["metric"],
            attrs=bench["quality_attrs"],
            clock=bench["clock"], reset=bench["reset"], reset_active_low=bench["reset_active_low"],
        )

    cone_v = bench_work / "extracted_cone.v"
    shell_v = bench_work / "top_shell.v"
    meta_json = bench_work / "cone_meta.json"

    through = bool(spec.through_registers and os.environ.get("AXFPGA_THROUGH_REGISTERS") != "0")
    extractor = ConeExtractor(annotated_files, top_module=top_mod, through_registers=through)
    meta = extractor.extract_safe_cone(
        submod_tag=f"{top_mod}_axc_cone_0",
        out_submod_path=cone_v,
        out_top_path=shell_v,
        out_meta_path=meta_json,
    )
    logger.info("Cone extraction status: %s | Safe cells: %d", meta.get("status"), len(meta.get("safe_cells", [])))
    if bench["annotated"] and meta.get("relaxed_signals"):
        for sig in meta["relaxed_signals"]:      # the spec (attribute or --bound) is the single source of the budget
            sig["bound"] = bench["bound"]
        meta_json.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    approx_cone_v = bench_work / "champion_approx_cone.v"
    final_approx_top_v = bench_work / "final_approx_top.v"
    approx_map_v, approx_map_j = bench_work / "approx_mapped.v", bench_work / "approx_mapped.json"
    has_safe_cone = cone_v.exists() and len(meta.get("safe_cells", [])) > 0 and meta.get("status") not in ("NO_RELAX_SIGNALS", "TAINTED_EMPTY_SAFE_SET")
    cone_meta = meta_json if meta_json.exists() else None

    # Application stimulus (shared by every attempt) and quality constraints. These need the cone metadata
    # (axc_* attributes of the relaxed wire, clock / reset ports).
    ports = _with_meta_ports(ports, meta, bench["clock"], bench["reset"], bench["reset_active_low"])
    if evaluator is None:
        evaluator = _quality_evaluator(spec, bench, meta, ports)
    quality = _resolve_quality(evaluator, bench)
    vectors, ctx = evaluator.build_stimulus(bench_id)
    sim_kw = dict(top=top_mod, inputs=ports["inputs"], outputs=ports["outputs"], work_dir=app_dir,
                  clk=ports["clk"], rst=ports["rst"], rst_active_low=ports["rst_active_low"])
    skip = 16 if ports["clk"] else 0

    def exact_reference() -> Dict[str, Any]:
        norm = _normalize_rtl(exact_top_v, top_mod, app_dir / "exact_norm.v")
        return run_vector_sim([norm], vectors=vectors, tag="app_exact", **sim_kw)

    f_sim = pre.submit(exact_reference) if inner_jobs > 1 else None
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

        def synth() -> Dict[str, Any]:
            return estimator.evaluate_netlist(top_v, top_module=top_mod, work_dir=bench_work,
                                              out_mapped_vlog=map_v, out_mapped_json=map_j)

        def miter_check() -> Dict[str, Any]:
            return MiterVerifier(exact_vlog=exact_top_v, approx_vlog=top_v, meta_json=cone_meta,
                                 num_cycles=1000, enable_formal=False, sim_timeout=SIM_TIMEOUT_S).verify()

        def app_sim() -> Dict[str, Any]:
            norm = _normalize_rtl(top_v, top_mod, app_dir / f"approx_norm_{key}.v")
            return run_vector_sim([norm], vectors=vectors, tag=f"app_approx_{key}", **sim_kw)

        checkpoint()
        if need_savings_first:
            # A candidate without savings is dropped before the expensive checks: run them in sequence.
            a_res = synth()
            if a_res["dsps"] > exact_res["dsps"]:
                # More DSP blocks than the exact design is never a saving (a pre-mapped DSP cone in a -nodsp run).
                rec.update(lcs=a_res["estimated_lcs"], luts=a_res["total_luts"], _res=a_res, savings_ok=False)
                reason = f"uses more DSP blocks than the exact design (DSP {exact_res['dsps']}->{a_res['dsps']})"
                rec.update(outcome="rejected", reason=reason, err_ok=None)
                logger.info("  %s [%s]: rejected: %s", label, variant, reason)
                return rec
            savings_ok = (a_res["estimated_lcs"] + DSP_LC_WEIGHT * a_res["dsps"]
                          < exact_res["estimated_lcs"] + DSP_LC_WEIGHT * exact_res["dsps"]
                          or (a_res["estimated_lcs"] <= exact_res["estimated_lcs"] and a_res["total_luts"] < exact_res["total_luts"]
                              and a_res["dsps"] <= exact_res["dsps"]))
            if not savings_ok:
                rec.update(lcs=a_res["estimated_lcs"], luts=a_res["total_luts"], _res=a_res, savings_ok=False)
                no_sav = (f"no top-level savings (LC {exact_res['estimated_lcs']}->{a_res['estimated_lcs']}, "
                          f"LUT {exact_res['total_luts']}->{a_res['total_luts']})"
                          + (f", DSP {exact_res['dsps']}->{a_res['dsps']}" if exact_res["dsps"] or a_res["dsps"] else ""))
                rec.update(outcome="rejected", reason=no_sav, err_ok=None)
                logger.info("  %s [%s]: rejected: %s", label, variant, no_sav)
                return rec
            checkpoint()
            miter = miter_check()
            checkpoint()
            sim = app_sim()
        else:
            # Synthesis, miter and application simulation are independent subprocess steps: run them together.
            if inner_jobs > 1:
                with ThreadPoolExecutor(max_workers=3) as ap:
                    a_res, miter, sim = (f.result() for f in [ap.submit(g) for g in (synth, miter_check, app_sim)])
            else:
                a_res = synth()
                checkpoint()
                miter = miter_check()
                checkpoint()
                sim = app_sim()
            checkpoint()
        rec.update(lcs=a_res["estimated_lcs"], luts=a_res["total_luts"], _res=a_res)
        rec["savings_ok"] = a_res["estimated_lcs"] < exact_res["estimated_lcs"] or a_res["total_luts"] < exact_res["total_luts"]
        no_sav = (f"no top-level savings (LC {exact_res['estimated_lcs']}->{a_res['estimated_lcs']}, "
                  f"LUT {exact_res['total_luts']}->{a_res['total_luts']})")
        errs = evaluator.error_stats(bench_id, exact_sim["outputs"], sim["outputs"], bench["relax_wire"], skip)
        app = evaluator.app_metrics(bench_id, vectors, exact_sim["outputs"], sim["outputs"],
                                       relax_wire=bench["relax_wire"], skip=skip)
        fails = []
        if miter.get("control_integrity") != "PASSED":
            fails.append("control outputs corrupted")
        q_fails, q_recs = quality.evaluate(errs, None, app)     # every constraint must hold, fixed message order
        fails += q_fails
        rec.update(wce=errs["wce"], med=errs["med"], mred_pct=errs["mred"] * 100.0, app_value=app["value"],
                   err_ok=not fails, _errs=errs, _app=app, _quality=q_recs)
        if not fails:
            rec["_sim_out"] = sim["outputs"]      # kept for the held-out reuse; dropped for rejected candidates
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
                    rec["_app"]["name"], fmt(rec["_app"]["value"], 3), formal_ctrl, formal_wce)
        return {"name": rec["candidate"] + (f" [{rec['variant']}]" if rec["variant"] in variants else ""),
                "res": rec["_res"], "errs": e, "app": rec["_app"], "formal_ctrl": formal_ctrl, "formal_wce": formal_wce,
                "quality": rec["_quality"], "sim_out": rec.get("_sim_out")}

    ho_cache: Dict[str, Any] = {}

    def heldout_violations(rec: Dict[str, Any], top_v: Path, tag: str) -> List[str]:
        """Constraint violations of a candidate on the held-out stream (empty: passes, or no held-out stream)."""
        if not hasattr(evaluator, "build_heldout"):
            return []
        try:
            h = _heldout(evaluator, quality, bench_id, bench["relax_wire"], skip, vectors, exact_sim["outputs"],
                         rec.get("_sim_out"), app_dir / "exact_norm.v", top_v, top_mod, sim_kw, cache=ho_cache, tag=tag)
        except Exception as exc:                  # cannot be evaluated: do not lose the candidate
            logger.warning("Held-out gate for %s failed: %s", rec["candidate"], exc)
            return []
        return [] if h is None else list(h["violations"])

    # Stage 3a: design-level partial-product pruning of every array multiplier (UB1BPPG cells), before flattening.
    # Binary search over the anti-diagonal depth; with inner jobs the next two possible depths are evaluated
    # speculatively. The visited depths and the choice are identical to the serial search.
    exact_code = exact_top_v.read_text(encoding="utf-8", errors="ignore")
    ppgs = PpgPruner.detect_ppg_modules(exact_code)
    meta_mode = os.environ.get("AXFPGA_SEARCH", "meta").strip().lower() != "tournament"
    exploration: Optional[Dict[str, Any]] = None
    tr = meta.get("through_registers") if through else None
    if meta_mode:
        accepted, fallback_reason, exploration = _explore_stage(
            bench=bench, bench_work=bench_work, exact_top_v=exact_top_v, exact_code=exact_code, ppgs=ppgs, tr=tr,
            annotated_files=annotated_files, top_mod=top_mod, cone_v=cone_v, shell_v=shell_v, cone_meta=cone_meta,
            has_safe_cone=has_safe_cone, quality=quality, estimator=estimator,
            cone_estimator=tournament_estimator or FpgaEstimator(family="xc7", device=FPGA_DEVICE,
                                                                 nodsp=getattr(estimator, "nodsp", False)),
            exact_res=exact_res, evaluator=evaluator, sim_kw=sim_kw, skip=skip, inner_jobs=inner_jobs,
            assess=assess, finalize=finalize, attempts=attempts, approx_cone_v=approx_cone_v)
    if ppgs and not meta_mode:
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
        # A PPG-pruned design has no WCE proof: its errors are only sampled. The cheapest passing depth is therefore
        # also checked on the held-out stream (never used for the search itself), and the next cheapest is taken if it fails.
        passing = sorted((r["lcs"], r["luts"], -k, k) for k, r in evaluated.items() if r["err_ok"] and r["savings_ok"])
        best_k = None
        for _lcs, _luts, _nk, k in passing:
            viol = heldout_violations(evaluated[k], ppg_dir / f"design_ppg_k{k}.v", f"heldout_ppg{k}")
            if viol:
                evaluated[k].update(outcome="rejected", reason="held-out stream: " + "; ".join(viol))
                logger.info("  ppg_design_k%d rejected on the held-out stream: %s", k, "; ".join(viol))
                continue
            best_k = k
            break
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
    tournament_rec: Optional[Dict[str, Any]] = None
    if meta_mode:
        pass
    elif accepted is None and not has_safe_cone:
        logger.warning("No safe cells extracted for %s (status: %s)", top_mod, meta.get("status"))
        fallback_reason = "no safe cone extracted" + (" and no in-budget PPG depth with savings" if ppgs else "")
    elif accepted is None:
        logger.info("[Stage 3b] pyALS Candidate Tournament on the safe cone...")
        pyals_dir = bench_work / "pyals_run"
        bridge = PyAlsBridge(cone_verilog_path=cone_v, meta_json_path=cone_meta, work_dir=pyals_dir, cut_size=4)
        tournament = Tournament(estimator=tournament_estimator or FpgaEstimator(
                                    family="xc7", device=FPGA_DEVICE, nodsp=getattr(estimator, "nodsp", False)),
                                work_dir=pyals_dir, sim_samples=TOURNAMENT_SAMPLES, jobs=inner_jobs)
        champion = tournament.run(cone_vlog=cone_v, meta_json=cone_meta, metric=bench["metric"],
                                  bound=bench["bound"], generators=[bridge, OperatorGenerator.from_env()])
        tournament_rec = {"baseline_lcs": champion.baseline_lcs, "baseline_luts": champion.baseline_luts,
                          "samples": TOURNAMENT_SAMPLES, "bound": champion.bound, "metric": champion.metric,
                          "entries": [{"name": e.name, "error": e.error, "within_bound": e.passed_bound,
                                       "lcs": e.lcs, "luts": e.luts} for e in champion.leaderboard]}
        attempt_limit = MAX_ATTEMPTS_SMALL if exact_res["estimated_lcs"] <= ATTEMPT_SMALL_LCS else MAX_ATTEMPTS
        alts, gaps = _ladder(list(champion.alternatives), attempt_limit)
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
                                                     cone_a, cone_meta, top_mod,
                                                     check_wce=bench["metric"].upper() != "MED")
            rec["_formal"] = (formal_ctrl, formal_wce)
            if "FALSIFIED" in (formal_ctrl, formal_wce):
                rec.update(outcome="rejected", reason=f"formal control={formal_ctrl}, WCE={formal_wce}")
                logger.info("  rejected: %s", rec["reason"])
            return rec

        jobs = [(i, alt, variant) for i, (alt, variant) in enumerate((a, v) for a in alts for v in variants)]
        with _stage_pool(inner_jobs) as (pool, cancel):
            consumed, found = first_accepted(jobs, lambda j: attempt(j, cancel), lambda r: r.get("outcome") == "passed checks",
                                             executor=pool if inner_jobs > 1 else None, cancel=cancel)
        attempts.extend(rec for _, rec in consumed)
        if found is not None:
            # The first candidate that passed may sit below milder-failing ones' neighbours: try the skipped, cheaper ones
            # between it and the previous attempt (cheapest first) and keep the cheapest that passes.
            passed_alt = consumed[found][0][1]                     # consumed items are the job tuples (index, candidate, variant)
            gap = gaps[alts.index(passed_alt)] if passed_alt in alts else []
            if gap:
                logger.info("[Attempt] %d skipped candidate(s) between %s and the previous attempt", len(gap), passed_alt.name)
                jobs2 = [(len(jobs) + i, alt, variant) for i, (alt, variant) in enumerate((a, v) for a in gap for v in variants)]
                with _stage_pool(inner_jobs) as (pool2, cancel2):
                    consumed2, found2 = first_accepted(jobs2, lambda j: attempt(j, cancel2), lambda r: r.get("outcome") == "passed checks",
                                                       executor=pool2 if inner_jobs > 1 else None, cancel=cancel2)
                attempts.extend(rec for _, rec in consumed2)
                if found2 is not None:
                    consumed, found = consumed2, found2
            rec = consumed[found][1]
            top_a, map_v, map_j, cone_a = rec["_paths"]
            shutil.copy(cone_a, approx_cone_v)
            accepted = finalize(rec, top_a, map_v, map_j, *rec["_formal"])
        if accepted is None:
            fallback_reason = (f"none of {len(alts)} cone candidate(s) x {len(variants)} variant(s)"
                               + (" or PPG depths" if ppgs else "") + " met every threshold (see attempts)") if alts else \
                              ("no cone candidate met the cone bound with resource savings" + (" and no in-budget PPG depth with savings" if ppgs else ""))

    # Stage 3c: operand truncation of the arithmetic behind internal registers (design level, no cone-level bound).
    # Candidates are evaluated like the PPG depths: application-stream limits, held-out gate, control proof; no WCE proof,
    # because the error behind a register accumulates across cycles (sequential bounds are Phase 6).
    reg_info: Optional[Dict[str, Any]] = None
    if tr is not None:
        reg_info = {"relaxed_registers": tr["num_registers_relaxed"], "arithmetic_sites": len(tr["sites"]),
                    "sites_behind_registers": len(tr["sites_behind_registers"]), "tried": 0, "accepted": None}
    if tr and tr["sites_behind_registers"] and not meta_mode:
        reg_dir = bench_work / "behind_registers"
        menu = {f"k{k}_{c}": {"k": k, "comp": c} for k in REG_KS for c in ("zero", "mid")}
        logger.info("[Stage 3c] Truncation behind registers: %d arithmetic site(s) (%d behind a register), %d candidates",
                    len(tr["sites"]), len(tr["sites_behind_registers"]), len(menu))
        dl = run_design_level([str(f) for f in annotated_files], top_mod, tr["sites"], menu, str(reg_dir))
        for name, err in dl["errors"].items():
            logger.warning("behind-register candidate %s failed: %s", name, err)
        cands = sorted(dl["written"].items())

        def eval_reg(item: Tuple[str, str], cancel: threading.Event) -> Dict[str, Any]:
            name, path = item
            rec = assess(Path(path), f"reg_{name}", "design", reg_dir / f"{name}_mapped.v", reg_dir / f"{name}_mapped.json",
                         need_savings_first=False, cancel=cancel, key=f"reg_{name}")
            rec.update(_top=Path(path))
            return rec

        with _stage_pool(inner_jobs) as (pool, cancel):
            futs = [pool.submit(eval_reg, it, cancel) for it in cands]
            reg_recs = [f.result() for f in futs]
        attempts.extend(reg_recs)
        reg_info["tried"] = len(reg_recs)
        passing = sorted((r["lcs"], r["luts"], r["candidate"]) for r in reg_recs if r["err_ok"] and r["savings_ok"])
        by_name = {r["candidate"]: r for r in reg_recs}
        best_reg = None
        for _lcs, _luts, cname in passing:
            rec = by_name[cname]
            if accepted is not None and rec["lcs"] >= accepted["res"]["estimated_lcs"]:
                break                                            # not better than what the cone path already accepted
            viol = heldout_violations(rec, rec["_top"], "heldout_" + cname)
            if viol:
                rec.update(outcome="rejected", reason="held-out stream: " + "; ".join(viol))
                logger.info("  %s rejected on the held-out stream: %s", cname, "; ".join(viol))
                continue
            ctrl = _formal_control(exact_top_v, rec["_top"], cone_meta)
            if ctrl == "FALSIFIED":
                rec.update(outcome="rejected", reason="formal control=FALSIFIED")
                continue
            best_reg = (rec, ctrl)
            break
        if best_reg is not None:
            rec, ctrl = best_reg
            nm = rec["candidate"][len("reg_"):]
            accepted = finalize(rec, rec["_top"], reg_dir / f"{nm}_mapped.v", reg_dir / f"{nm}_mapped.json", ctrl,
                                "N/A (truncation behind registers; errors measured on application stream)")
            reg_info["accepted"] = rec["candidate"]
        else:
            logger.info("  no behind-register candidate passed every check")
        if accepted is None and not has_safe_cone:
            fallback_reason = "no safe cone extracted and no behind-register candidate passed"

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
        app = evaluator.app_metrics(bench_id, vectors, exact_sim["outputs"], exact_sim["outputs"],
                                       relax_wire=bench["relax_wire"], skip=skip)
        formal_ctrl = formal_wce = "N/A (exact design kept)"
        _, q_recs = quality.evaluate(evaluator.error_stats(bench_id, exact_sim["outputs"], exact_sim["outputs"],
                                                           bench["relax_wire"], skip), None, app)
    else:
        approx_res, method, errs, app = accepted["res"], accepted["name"], accepted["errs"], accepted["app"]
        formal_ctrl, formal_wce = accepted["formal_ctrl"], accepted["formal_wce"]
        q_recs = accepted["quality"]
    app["note"] = ctx.get("note", "")
    app["samples"] = len(vectors)
    errors_full: Optional[Dict[str, Any]] = None
    try:
        from axfpga.quality.metrics import decoded as _decoded
        a_rows = exact_sim["outputs"] if fell_back else (accepted.get("sim_out") or exact_sim["outputs"])
        n_rows = min(len(exact_sim["outputs"]), len(a_rows))
        e_col = _decoded(evaluator.fmt, exact_sim["outputs"], bench["relax_wire"], skip, n_rows)
        a_col = _decoded(evaluator.fmt, a_rows, bench["relax_wire"], skip, n_rows)
        errors_full = error_metrics(e_col, a_col, int(getattr(evaluator.fmt, "width", 0) or 0))
        errors_full["domain"] = "application stream, relaxed output decoded in its number format"
    except Exception as exc:
        logger.warning("full error metrics unavailable: %s", exc)

    # Stage 7b: held-out stream, evaluated on the accepted design and reported. It is used only as an acceptance gate
    # for PPG-pruned designs (Stage 3a), never for the search.
    heldout: Optional[Dict[str, Any]] = None
    heldout_reason: Optional[str] = None
    if fell_back:
        heldout_reason = "exact design kept; nothing to evaluate"
    elif meta_mode:
        heldout_reason = "not evaluated (design-space exploration mode)"
    elif not hasattr(evaluator, "build_heldout"):
        heldout_reason = "evaluator provides no held-out stream"
    else:
        try:
            logger.info("[Stage 7b] Held-out stream evaluation of the accepted design...")
            heldout = _heldout(evaluator, quality, bench_id, bench["relax_wire"], skip, vectors, exact_sim["outputs"],
                               accepted["sim_out"], app_dir / "exact_norm.v", final_approx_top_v, top_mod, sim_kw,
                               cache=ho_cache)
        except Exception as exc:                  # reported, not enforced: a held-out failure must not lose the design
            logger.warning("Held-out evaluation failed: %s", exc)
            heldout_reason = f"held-out evaluation failed: {exc}"
    if accepted is not None:
        accepted.pop("sim_out", None)

    # Stage 8: activity-based power of mapped netlists
    logger.info("[Stage 8] Activity-based dynamic power @ %.0f MHz...", F_CLK_MHZ)
    pvecs = power_cap(evaluator.power_vectors(bench_id, vectors), POWER_CYCLES)
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
    out_bits = dict(ports["outputs"]).get(bench["relax_wire"])   # width of the relaxed output (full scale = 2^bits - 1)
    return {
        "id": bench_id,
        "name": bench["name"],
        "domain": bench["domain"],
        "top": top_mod,
        "device": part,
        "dsp_inference": not getattr(estimator, "nodsp", False),
        "engine": "design-space exploration (mealpy)" if meta_mode else "pyALS Candidate Tournament",
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
            "out_bits": out_bits,
            "wce_pct": round(100.0 * errs["wce"] / ((1 << out_bits) - 1), 6) if out_bits else None,
            "domain": "application stream, relaxed output in its number format (LSB units)",
        },
        "formal": {"control_equivalence": formal_ctrl, "wce_bound": formal_wce},
        "certification": certification_record(formal_ctrl, formal_wce, exact_design=fell_back),
        "limits": {"wce": bench["bound"], "med": quality.med_limit,
                   "mred_pct": None if quality.mred_limit is None else quality.mred_limit * 100.0, "app": app["threshold"]},
        "quality": q_recs,
        "heldout": heldout,
        "heldout_reason": heldout_reason,
        "tournament": tournament_rec,
        "exploration": exploration,
        "errors_full": errors_full,
        "through_registers": reg_info,
        "attempts": attempts,
        "app": app,
        "power_model": {k: exact_pwr[k] for k in ("method", "f_clk_mhz", "energy_pj")} | {"cycles": exact_pwr["cycles"]},
    }


EXPLORE_TRIES = 5          # archive points tried per technique when the synthesized design fails a check (app metric)
NO_PROOF = "N/A (design-space exploration: no proofs run)"


def _explore_stage(*, bench, bench_work, exact_top_v, exact_code, ppgs, tr, annotated_files, top_mod, cone_v, shell_v,
                   cone_meta, has_safe_cone, quality, estimator, cone_estimator, exact_res, evaluator, sim_kw, skip,
                   inner_jobs, assess, finalize, attempts, approx_cone_v):
    """Design-space exploration of C_safe, one search per technique, then the synthesized check of each technique's
    best points (full synthesis, control sanity simulation, application stream and metric). Winner: lowest cost among
    the techniques whose design passed. If none passed: one re-search with the error limits halved, then exact."""
    ex_dir = bench_work / "explore"
    algorithm = algorithm_from_env()
    only = techniques_from_env()
    primary = "med" if str(bench["metric"]).upper() == "MED" else "wce"
    limits = {c.metric: c.threshold for c in quality.constraints if c.metric in LOWER_METRICS and c.op in ("<=", "<")}
    exact_cost = exact_res["estimated_lcs"] + DSP_LC_WEIGHT * exact_res["dsps"]
    info: Dict[str, Any] = {"algorithm": algorithm, "primary_metric": primary, "limits": limits, "techniques": [],
                            "winner": None, "re_search": False, "base_cost": exact_cost}

    def build(lim: Dict[str, float], tag: str) -> Tuple[List[Any], Dict[str, Any]]:
        evaluators: Dict[str, Any] = {}
        techs: List[Any] = []
        if has_safe_cone:
            try:
                cev = ConeEvaluator(cone_v, cone_meta, ex_dir / tag / "cone", cone_estimator, lim)
                evaluators["cone"] = cev
                techs += cone_techniques(cev, cone_v, cone_meta, ex_dir / tag, float(bench["bound"]), only)
            except Exception as exc:
                logger.warning("cone exploration unavailable: %s", exc)
        want = (lambda n: only is None or n in only)
        reg_sites = list(tr["sites"]) if (tr and tr.get("sites_behind_registers")) else []
        if (ppgs and want("ppg_design")) or (reg_sites and want("trunc_reg")):
            try:
                dev = DesignEvaluator(exact_top_v, top_mod, ex_dir / tag / "design", estimator, lim,
                                      dict(sim_kw), evaluator.fmt, bench["relax_wire"], skip,
                                      lambda src, out: _normalize_rtl(src, top_mod, out), exact_res)
                evaluators["design"] = dev
                if ppgs and want("ppg_design"):
                    k_max = max(i + j for i, j in ppgs) + 1
                    techs.append(PpgDesignTechnique(exact_code, k_max, PpgPruner(work_dir=ex_dir / tag / "ppg")))
                if reg_sites and want("trunc_reg"):
                    techs.append(RegTechnique([str(f) for f in annotated_files], top_mod, reg_sites))
            except Exception as exc:
                logger.warning("design-level exploration unavailable: %s", exc)
        return techs, evaluators

    def check(summaries: List[Dict[str, Any]], tag: str) -> List[Dict[str, Any]]:
        """Synthesize and check the best points of every technique (in parallel over techniques)."""
        cdir = bench_work / "explore_check" / tag

        def one(s: Dict[str, Any]) -> Dict[str, Any]:
            res = {"technique": s["technique"], "tries": 0, "passed": False, "rec": None, "reasons": []}
            for i, row in enumerate([r for r in s["ranked"] if r["cost"] is not None and r["cost"] < s["base_cost"]][:EXPLORE_TRIES]):
                res["tries"] += 1
                adir = cdir / f"{s['technique']}_{i}"
                adir.mkdir(parents=True, exist_ok=True)
                label = f"{s['technique']}: {row['describe']}"[:160]
                if s["level"] == "cone":
                    cone_a, top_a = adir / "approx_cone.v", adir / "final_top.v"
                    shutil.copy(row["path"], cone_a)
                    try:
                        _reintegrate(bench, adir, shell_v, cone_v, cone_a, top_a)
                    except Exception as exc:
                        res["reasons"].append(f"reintegration failed: {exc}"[:200])
                        continue
                else:
                    cone_a, top_a = None, Path(row["path"])
                map_v, map_j = adir / "approx_mapped.v", adir / "approx_mapped.json"
                try:
                    rec = assess(top_a, label, "explore", map_v, map_j, need_savings_first=True,
                                 cancel=threading.Event(), key=f"x_{tag}_{s['technique']}_{i}")
                except Exception as exc:
                    res["reasons"].append(f"check failed: {exc}"[:200])
                    continue
                rec.update(technique=s["technique"], search_cost=row["cost"], genome=row["genome"],
                           _paths=(top_a, map_v, map_j, cone_a))
                attempts.append(rec)
                if rec.get("outcome") == "passed checks":
                    res.update(passed=True, rec=rec)
                    break
                res["reasons"].append(rec.get("reason", "rejected"))
            return res

        with ThreadPoolExecutor(max_workers=max(1, min(inner_jobs, 4))) as pool:
            return list(pool.map(one, summaries))

    techs, evs = build(limits, "main")
    logger.info("[Explore] %d technique(s): %s | algorithm %s | limits %s", len(techs), ", ".join(t.name for t in techs),
                algorithm, limits)
    summaries = explore(techs, evs, ex_dir / "main", algorithm, inner_jobs, primary)
    checks = check(summaries, "main")
    if not any(c["passed"] for c in checks) and any(s["best"] for s in summaries):
        half = {k: v / 2.0 for k, v in limits.items()}
        logger.info("[Explore] no synthesized point passed every check: re-search with halved limits %s", half)
        info["re_search"] = True
        techs2, evs2 = build(half, "half")
        keep = {s["technique"] for s in summaries if s["best"]}
        summaries2 = explore([t for t in techs2 if t.name in keep], evs2, ex_dir / "half", algorithm, inner_jobs, primary)
        checks2 = check(summaries2, "half")
        for s in summaries2:
            s["technique_run"] = "half-limit re-search"
        summaries, checks = summaries + summaries2, checks + checks2
    by_tech = {c["technique"]: c for c in checks}
    for s in summaries:
        c = by_tech.get(s["technique"]) or {}
        rec = c.get("rec")
        s["synthesized"] = None if rec is None else {
            "candidate": rec["candidate"], "lcs": rec["lcs"], "luts": rec["luts"],
            "ffs": rec["_res"]["ffs"], "dsps": rec["_res"]["dsps"], "carry4": rec["_res"]["carry4"],
            "cost": rec["lcs"] + DSP_LC_WEIGHT * rec["_res"]["dsps"], "wce": rec.get("wce"), "med": rec.get("med"),
            "mred_pct": rec.get("mred_pct"), "app_value": rec.get("app_value")}
        s["check"] = {"tries": c.get("tries", 0), "passed": c.get("passed", False), "reasons": c.get("reasons", [])[:5]}
        info["techniques"].append(s)
    passed = [c["rec"] for c in checks if c["passed"]]
    if not passed:
        reason = ("no technique found a feasible point with savings" if not any(s["best"] for s in summaries)
                  else "no synthesized point passed the full-design checks (see exploration)")
        return None, reason, info
    win = min(passed, key=lambda r: (r["lcs"] + DSP_LC_WEIGHT * r["_res"]["dsps"], r.get("wce") or 0, r["candidate"]))
    top_a, map_v, map_j, cone_a = win["_paths"]
    if cone_a is not None:
        shutil.copy(cone_a, approx_cone_v)
    info["winner"] = {"technique": win["technique"], "candidate": win["candidate"], "genome": win.get("genome")}
    acc = finalize(win, top_a, map_v, map_j, NO_PROOF, NO_PROOF)
    return acc, None, info


def _ladder(alts: List[Any], limit: int) -> Tuple[List[Any], List[List[Any]]]:
    """The candidates to attempt, best (cheapest) first, and the ones skipped between consecutive attempts.

    Greedy stacking makes many candidates of almost the same cost whose errors rise together: the first ``limit`` by cost
    would be variants of one aggressive genome that all fail the application-stream check, and the milder candidates that
    would pass are never tried. So: one candidate per distinct cone cost, spread evenly over the whole range when there
    are more than ``limit`` of them (always including the cheapest and the mildest), then cost ties up to ``limit``.
    ``gaps[i]`` holds the distinct-cost candidates skipped between attempt i - 1 and attempt i: when attempt i is the
    first to pass, they are tried next (cheapest first), because one of them may pass too and save more.
    With at most ``limit`` distinct costs this is the plain best-first prefix and every gap is empty.
    """
    seen, distinct, ties = set(), [], []
    for a in alts:                       # alternatives are already sorted by cost, luts, error, name
        key = a.lcs + DSP_LC_WEIGHT * a.dsps
        (ties if key in seen else distinct).append(a)
        seen.add(key)
    n = len(distinct)
    if n <= limit:
        return (distinct + ties)[:limit], [[] for _ in range(min(limit, n + len(ties)))]
    picks = sorted({round(j * (n - 1) / (limit - 1)) for j in range(limit)}) if limit > 1 else [0]
    chosen = [distinct[i] for i in picks]
    gaps = [[]] + [distinct[picks[j - 1] + 1:picks[j]] for j in range(1, len(picks))]
    return chosen, gaps


def _spread_attempts(alts: List[Any], limit: int) -> List[Any]:
    """The candidates to attempt first (see ``_ladder``)."""
    return _ladder(alts, limit)[0]


def _resolve_quality(evaluator: Any, bench: Dict[str, Any]) -> QualitySpec:
    """The constraint set of the run: the evaluator's attribute constraints, else the legacy budget fields.

    The formal bound (``bench["bound"]``, from --bound or the attributes) always wins for the wce constraint.
    """
    q = getattr(evaluator, "_quality", None)
    if q is None:
        return QualitySpec.legacy(bench["bound"], bench["metric"], bench["med_limit"], bench["mred_limit"])
    if q.wce_bound is not None and q.wce_bound != float(bench["bound"]):
        logger.warning("wce<=%g of axc_quality replaced by the bound %g of the run", q.wce_bound, bench["bound"])
        q = q.with_wce(float(bench["bound"]))
        evaluator._quality = q
    return q


def _quality_evaluator(spec: DesignSpec, bench: Dict[str, Any], meta: Dict[str, Any], ports: Dict[str, Any]):
    """QualityEvaluator from the attributes of the relaxed wire in the cone metadata (plus DesignSpec overrides)."""
    wire = bench["relax_wire"]
    sig = next((s for s in meta.get("relaxed_signals") or [] if s.get("wire") == wire), None) or {}
    width = dict(ports["outputs"]).get(wire)
    if width is None:
        raise ValueError(f"relaxed signal {wire!r} is not an output port of {spec.top}; error statistics need a top output")
    if sig.get("quality"):
        wq = WireQuality.from_dict(sig["quality"])
    else:
        wq = WireQuality(wire=wire, width=width, bound=bench["bound"])
    over = {k: v for k, v in (("stimulus", spec.stimulus), ("plugin", spec.plugin), ("seed", spec.seed)) if v is not None}
    if over:
        wq = dataclasses.replace(wq, **over)
    search = [*spec.search_dirs]
    return make_evaluator(wq, design_id=spec.id, top=spec.top, inputs=ports["inputs"], outputs=ports["outputs"],
                          has_clock=bool(ports["clk"]), base_dir=Path(bench["files"][0]).resolve().parent,
                          search_dirs=search, default_samples=spec.samples, signed=spec.signed,
                          quality=wq.quality)


def _heldout(evaluator: Any, quality: QualitySpec, bench_id: str, wire: str, skip: int, tuning_vectors, exact_out,
             approx_out, exact_norm: Path, approx_top: Path, top_mod: str, sim_kw: Dict[str, Any],
             cache: Optional[Dict[str, Any]] = None, tag: str = "heldout") -> Optional[Dict[str, Any]]:
    """Exact and accepted-approximate simulation on the held-out stream; error statistics, constraints, application metric.

    ``cache`` keeps the held-out stimulus and the exact outputs across calls (the gate evaluates several candidates);
    ``tag`` keeps the file names of different candidates apart."""
    cache = cache if cache is not None else {}
    if "built" not in cache:
        cache["built"] = evaluator.build_heldout(bench_id)
    built = cache["built"]
    if built is None:
        return None
    hv, hctx = built
    if hv == tuning_vectors:                       # same stream (e.g. an exhaustive table): reuse the tuning outputs
        h_exact, h_approx = exact_out, approx_out
    else:
        if "exact" not in cache:
            cache["exact"] = run_vector_sim([exact_norm], vectors=hv, tag="heldout_exact", **sim_kw)["outputs"]
        h_exact = cache["exact"]
        norm = _normalize_rtl(approx_top, top_mod, Path(sim_kw["work_dir"]) / f"{tag}_approx_norm.v")
        h_approx = run_vector_sim([norm], vectors=hv, tag=f"{tag}_approx", **sim_kw)["outputs"]
    errs = evaluator.error_stats(bench_id, h_exact, h_approx, wire, skip)
    app = evaluator.app_metrics_for("heldout", hv, h_exact, h_approx, skip)
    fails, recs = quality.evaluate(errs, None, app)
    app["note"], app["samples"] = hctx.get("note", ""), len(hv)
    return {"samples": len(hv), "note": hctx.get("note", ""),
            "errors": {"wce": errs["wce"], "med": round(errs["med"], 4), "mred_pct": round(errs["mred"] * 100.0, 4),
                       "er": round(errs["er"], 4)},
            "quality": recs, "app": app, "within_limits": not fails, "violations": fails}


def fmt(v: Any, nd: int = 2) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "inf"
        return f"{v:.{nd}f}"
    return str(v)


class GenericEvaluator:
    """Evaluator for arbitrary designs: seeded random stimulus, error statistics of one relaxed output, no application metric.

    Error is the modular distance of the two output words, so two's-complement wrap-around is handled and no
    signedness is needed; ``signed`` only selects the reference for MRED. 1-bit inputs named like an enable are
    mostly 1, those named like a clear/flush mostly 0, so state-holding designs accumulate.
    """
    _ON = re.compile(r"(^|_)(en|enable|valid|start|ce|run|load)($|_)", re.I)
    _OFF = re.compile(r"(^|_)(clear|clr|flush|zero|reset|rst)($|_)", re.I)

    def __init__(self, inputs: Sequence[Tuple[str, int]], width: int, n_vectors: int = 1000, seed: int = 1,
                 signed: bool = False):
        self.inputs, self.width, self.n_vectors, self.seed, self.signed = list(inputs), width, n_vectors, seed, signed

    def build_stimulus(self, bench_id: str) -> Tuple[List[Dict[str, int]], Dict[str, Any]]:
        import random
        rng = random.Random(self.seed)
        vecs: List[Dict[str, int]] = []
        for _ in range(self.n_vectors):
            v = {}
            for n, w in self.inputs:
                if w == 1 and self._ON.search(n):
                    v[n] = int(rng.random() < 0.9)
                elif w == 1 and self._OFF.search(n):
                    v[n] = int(rng.random() < 0.05)
                else:
                    v[n] = rng.getrandbits(w)
            vecs.append(v)
        return vecs, {"note": f"seeded random stimulus ({self.n_vectors} vectors, seed {self.seed})"}

    def error_stats(self, bench_id: str, exact: List[Dict[str, int]], approx: List[Dict[str, int]],
                    wire: str, skip: int) -> Dict[str, float]:
        mod, half = 1 << self.width, 1 << (self.width - 1)
        n = min(len(exact), len(approx))
        errs, mreds = [], []
        for e_row, a_row in zip(exact[skip:n], approx[skip:n]):
            e, a = e_row[wire] % mod, a_row[wire] % mod
            d = (e - a) % mod
            d = min(d, mod - d)
            ref = abs(e - mod if (self.signed and e >= half) else e)
            errs.append(d)
            if ref:
                mreds.append(d / ref)
        k = len(errs)
        return {"wce": float(max(errs)) if k else 0.0, "med": sum(errs) / k if k else 0.0,
                "mred": sum(mreds) / len(mreds) if mreds else 0.0, "er": sum(1 for d in errs if d) / k if k else 0.0,
                "samples": k, "format": "signed" if self.signed else "unsigned"}

    def app_metrics(self, bench_id: str, vectors: List[Dict[str, int]], exact: List[Dict[str, int]],
                    approx: List[Dict[str, int]], relax_wire: str = "", skip: int = 0) -> Dict[str, Any]:
        return {"name": "none", "value": None, "unit": "", "threshold": None, "pass": None}

    def power_vectors(self, bench_id: str, vectors: List[Dict[str, int]]) -> List[Dict[str, int]]:
        return vectors
