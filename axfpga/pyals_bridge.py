"""pyALS Approximate Logic Synthesis Bridge for AxFPGA.

Integrates Salvatore Barone's Catalog-based AIG-rewriting ALS engine (pyALS / pyALSlib / pyALS-lut-catalog)
as the native FPGA approximation engine for AxFPGA, replacing the ASIC-centric AxLS.

Key Capabilities:
1. Native 4-LUT / K-LUT Graph Extraction via Pyosys (YosysHelper, ALSGraph).
2. Approximate LUT Catalog querying (ALSCatalogCache & lut_catalog.db).
3. Selective AIG Rewriting substituting multi-gate sub-trees with approximate lower-depth / lower-gate LUTs.
4. AMOSA Multi-Objective Optimization / Direct Distance-k Rewriting under error bounds (MED, WCE, ER).
5. Seamless integration with AxFPGA's Xilinx 7-Series Artix-7 Estimator and Tournament Arbiter.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from pyosys import libyosys as ys

# Core pyalslib imports
import pyalslib
from pyalslib import ALSCatalogCache, ALSGraph, YosysHelper

from axfpga.estimator import FpgaEstimator

logger = logging.getLogger("axfpga.pyals_bridge")

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "lut_catalog.db"
if not DEFAULT_CATALOG_PATH.exists():
    ALT_PATH = Path(__file__).resolve().parent.parent / "pyALS-lut-catalog" / "lut_catalog.db"
    if ALT_PATH.exists():
        DEFAULT_CATALOG_PATH = ALT_PATH


class PyAlsBridge:
    """Bridge coordinating pyALS and pyALSlib approximate logic synthesis for AxFPGA."""

    def __init__(
        self,
        cone_verilog_path: str | Path,
        meta_json_path: Optional[str | Path] = None,
        work_dir: Optional[str | Path] = None,
        catalog_db_path: Optional[str | Path] = None,
        cut_size: int = 4,
        solver: str = "z3",
        timeout: int = 60000,
        target_family: str = "xc7",
        target_device: str = "xc7a35t",
        nodsp: bool = False,
        sim_samples: int = 256,
    ):
        self.cone_vlog = Path(cone_verilog_path).resolve()
        self.meta_json = Path(meta_json_path).resolve() if meta_json_path else None
        self.work_dir = Path(work_dir).resolve() if work_dir else Path(tempfile.mkdtemp(prefix="pyals_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)

        if catalog_db_path and Path(catalog_db_path).exists():
            self.catalog_db = Path(catalog_db_path).resolve()
        else:
            self.catalog_db = DEFAULT_CATALOG_PATH

        self.cut_size = cut_size
        self.solver = solver
        self.timeout = timeout
        self.target_family = target_family.lower()
        self.target_device = target_device.lower()
        self.nodsp = nodsp
        self.sim_samples = sim_samples

        self.estimator = FpgaEstimator(family=self.target_family, device=self.target_device, nodsp=self.nodsp)
        self.cache = ALSCatalogCache(str(self.catalog_db))

    def _parse_cone_interface(self) -> Dict[str, Any]:
        """Extract module name, ports, widths, and relaxed signal info."""
        content = self.cone_vlog.read_text(encoding="utf-8")
        mod_m = re.search(r"module\s+(\w+)\s*\((.*?)\);", content, re.DOTALL)
        if not mod_m:
            raise ValueError(f"Could not parse module header from {self.cone_vlog}")

        mod_name = mod_m.group(1)
        raw_ports = [p.strip() for p in mod_m.group(2).split(",") if p.strip()]

        # Scope content strictly to the target module to avoid bleeding ports from sister modules
        end_idx = content.find("endmodule")
        mod_content = content[:end_idx + len("endmodule")] if end_idx != -1 else content

        inputs: Dict[str, Dict[str, Any]] = {}
        outputs: Dict[str, Dict[str, Any]] = {}

        port_pat = re.compile(r"\b(input|output|inout)\s+(?:wire\s+|reg\s+)?(?:signed\s+)?(?:\[([^\]]+)\]\s*)?(\S+)")
        for line in mod_content.splitlines():
            line = line.strip()
            m = port_pat.search(line)
            if m:
                direction = m.group(1)
                width_str = m.group(2)
                name = m.group(3).rstrip(";").strip()
                width = 1
                msb = 0
                lsb = 0
                if width_str:
                    w_m = re.match(r"\[?(\d+):(\d+)\]?", width_str.strip())
                    if w_m:
                        msb = int(w_m.group(1))
                        lsb = int(w_m.group(2))
                        width = abs(msb - lsb) + 1
                info = {"name": name, "width": width, "msb": msb, "lsb": lsb, "signed": False}
                if direction in ("input", "inout"):
                    inputs[name] = info
                if direction in ("output", "inout"):
                    outputs[name] = info

        # Select relaxed output: pick the widest multi-bit output (the main arithmetic bus)
        multi_outs = [k for k, v in outputs.items() if v["width"] > 1]
        if multi_outs:
            relaxed_out = max(multi_outs, key=lambda k: outputs[k]["width"])
        elif outputs:
            relaxed_out = list(outputs.keys())[-1]
        elif inputs:
            relaxed_out = list(inputs.keys())[-1]
        else:
            relaxed_out = "out"

        return {
            "module_name": mod_name,
            "ports": raw_ports,
            "inputs": inputs,
            "outputs": outputs,
            "relaxed_output": relaxed_out,
            "raw_content": mod_content,
        }

    def generate_catalog_candidates(self, bound: float = 16.0) -> List[Dict[str, Any]]:
        """Map cone to 4-LUT network and generate approximate candidate netlists from lut_catalog.db.
        
        Applies:
        - Phase 1.1: Pre-YosysHelper arithmetic extraction and 4-bit nibble splitting
        - Phase 1.2: Support-set and input-dropping filter prioritizing LUTs that drop inputs
        - Plan 2 (Pillar 1): LOA (Lower-part OR Adder) candidates
        - Plan 2 (Pillar 2): Wallace Multiplier PPG matrix truncation candidates
        """
        iface = self._parse_cone_interface()
        top_name = iface["module_name"]
        candidates: List[Dict[str, Any]] = []
        exact_clean_v = self.work_dir / "exact_cone_clean.v"
        raw_cone_content = exact_clean_v.read_text(encoding="utf-8") if exact_clean_v.exists() else (self.cone_vlog.read_text(encoding="utf-8") if self.cone_vlog.exists() else "")

        # Plan 2 (Pillar 1): Structural / Behavioral LOA candidates
        try:
            from axfpga.alu_splitter import AluSplitter
            loa_cands = AluSplitter.generate_loa_candidates(
                cone_content=raw_cone_content,
                iface=iface,
                bound=bound,
                work_dir=self.work_dir,
            )
            if loa_cands:
                candidates.extend(loa_cands)
                logger.info("Generated %d LOA adder/subtractor candidates", len(loa_cands))
        except Exception as exc:
            logger.warning("Could not generate LOA candidates: %s", exc)

        # Plan 2 (Pillar 2): Wallace Multiplier PPG matrix truncation candidates
        try:
            from axfpga.ppg_pruner import PpgPruner
            pruner = PpgPruner(work_dir=self.work_dir)
            ppg_cands = pruner.generate_candidates(
                cone_content=raw_cone_content,
                bound=bound,
                out_dir=self.work_dir,
                top_module=top_name,
            )
            if ppg_cands:
                candidates.extend(ppg_cands)
                logger.info("Generated %d PPG multiplier pruner candidates", len(ppg_cands))
        except Exception as exc:
            logger.warning("Could not generate PPG pruner candidates: %s", exc)

        # Phase 1.1: Pre-YosysHelper Arithmetic Extraction and 4-Bit Nibble Splitting
        # Protects wide $alu cells (W >= 8) so techmap does not shatter hard CARRY4 chains
        from axfpga.alu_splitter import AluSplitter
        design = ys.Design()
        lut_netlist = self.work_dir / "exact_lut_cone.v"
        
        cone_src = exact_clean_v if exact_clean_v.exists() else self.cone_vlog
        ys.run_pass(f"read_verilog -sv {cone_src}; hierarchy -check -top {top_name};", design)
        splitter = AluSplitter(min_width=8, approx_nibble_bits=4)
        split_count = splitter.protect_and_split(design, top_module_name=top_name, as_blackbox=True)
        if split_count > 0:
            logger.info("pyALS protected %d wide arithmetic chains on 4-bit nibble boundaries in %s", split_count, top_name)

        ys.run_pass(f"proc; opt_clean; techmap; opt; abc -lut {self.cut_size}; clean", design)
        AluSplitter.unblackbox_exact_modules(design)
        ys.run_pass(f"write_verilog -noattr {lut_netlist}", design)
        AluSplitter.fixup_verilog_file(lut_netlist)

        if not lut_netlist.exists():
            logger.warning("Yosys K-LUT mapping failed for pyALS cone: %s", lut_netlist)
            return candidates

        content = lut_netlist.read_text(encoding="utf-8")

        # Discover all LUT assignments and cells in the netlist:
        # Pattern A: assign wire = 16'h7800 >> { ... };
        lut_assign_pat = re.compile(
            r"assign\s+([A-Za-z0-9_\[\]\\]+)\s*=\s*(\d+)'h([0-9a-fA-F]+)\s*>>\s*\{([^}]+)\};"
        )
        # Pattern B: \$lut #( .LUT(16'b...), .WIDTH(4) ) cell ( ... );
        lut_cell_pat = re.compile(
            r"\\?\$lut\s*#\(\s*\.LUT\((\d+)'b([01]+)\),\s*\.WIDTH\((\d+)\)\s*\)\s*(\S+)\s*\((.*?)\);",
            re.DOTALL,
        )

        assign_matches = list(lut_assign_pat.finditer(content))
        cell_matches = list(lut_cell_pat.finditer(content))
        total_luts = len(assign_matches) + len(cell_matches)
        logger.info("pyALS extracted %d LUT elements (%d assigns, %d cells) from %s",
                    total_luts, len(assign_matches), len(cell_matches), top_name)

        if total_luts == 0:
            return candidates

        # -------------------------------------------------------------
        # Phase 1.2: Support-Set / Input-Dropping Filter (Pattern A)
        # -------------------------------------------------------------
        for dist in [1, 2, 3, 4]:
            mod_content = content
            num_replaced = 0

            for match in assign_matches:
                wire_name = match.group(1)
                width_bits = int(match.group(2))
                hex_str = match.group(3)
                inputs_str = match.group(4)

                bin_spec = bin(int(hex_str, 16))[2:].zfill(width_bits)[::-1]
                input_drop_luts = self.cache.get_input_dropping_approx_luts(bin_spec, max_dist=dist)
                if input_drop_luts:
                    best_cand = input_drop_luts[0]
                    synth_spec = best_cand["synth_spec"][::-1]
                    approx_hex = f"{int(synth_spec, 2):0{max(1, width_bits // 4)}x}"
                    rep_assign = f"assign {wire_name} = {width_bits}'h{approx_hex} >> {{{inputs_str}}};"
                    mod_content = mod_content.replace(match.group(0), rep_assign)
                    num_replaced += 1

            if num_replaced > 0:
                cand_v = self.work_dir / f"cand_pyals_input_drop_dist{dist}.v"
                cand_v.write_text(mod_content, encoding="utf-8")
                candidates.append({
                    "name": f"pyALS_input_drop_dist{dist}",
                    "path": cand_v,
                    "strategy": "pyals",
                    "distance": dist,
                    "replaced_luts": num_replaced,
                    "input_dropping": True,
                })
                logger.info("Generated Phase 1.2 Input-Dropping candidate 'pyALS_input_drop_dist%d' with %d rewritten LUTs",
                            dist, num_replaced)

        # Phase 1.2b: Targeted LSB input-dropping rewrites (only lowest k LUTs)
        for k in [1, 2, 3, 4]:
            if k <= len(assign_matches):
                for dist in [1, 2]:
                    mod_content = content
                    num_replaced = 0
                    for match in assign_matches[:k]:
                        wire_name = match.group(1)
                        width_bits = int(match.group(2))
                        hex_str = match.group(3)
                        inputs_str = match.group(4)
                        bin_spec = bin(int(hex_str, 16))[2:].zfill(width_bits)[::-1]
                        input_drop_luts = self.cache.get_input_dropping_approx_luts(bin_spec, max_dist=dist)
                        if input_drop_luts:
                            best_cand = input_drop_luts[0]
                            synth_spec = best_cand["synth_spec"][::-1]
                            approx_hex = f"{int(synth_spec, 2):0{max(1, width_bits // 4)}x}"
                            rep_assign = f"assign {wire_name} = {width_bits}'h{approx_hex} >> {{{inputs_str}}};"
                            mod_content = mod_content.replace(match.group(0), rep_assign)
                            num_replaced += 1
                    if num_replaced > 0:
                        cand_v = self.work_dir / f"cand_pyals_lsb_drop_{k}_dist{dist}.v"
                        cand_v.write_text(mod_content, encoding="utf-8")
                        candidates.append({
                            "name": f"pyALS_lsb_drop_{k}_dist{dist}",
                            "path": cand_v,
                            "strategy": "pyals",
                            "distance": dist,
                            "replaced_luts": num_replaced,
                        })

        # -------------------------------------------------------------
        # Phase 1.2: Support-Set / Input-Dropping Filter (Pattern B: $lut cells)
        # -------------------------------------------------------------
        for dist in [1, 2, 3, 4]:
            mod_content = content
            num_replaced = 0

            for match in cell_matches:
                width_bits = int(match.group(1))
                bin_spec = match.group(2)[::-1]
                cell_name = match.group(4)
                conn_str = match.group(5)

                input_drop_luts = self.cache.get_input_dropping_approx_luts(bin_spec, max_dist=dist)
                if input_drop_luts:
                    best_cand = input_drop_luts[0]
                    synth_spec = best_cand["synth_spec"][::-1]
                    rep_cell = f"\\$lut #( .LUT({width_bits}'b{synth_spec}), .WIDTH({width_bits}) ) {cell_name} ({conn_str});"
                    mod_content = mod_content.replace(match.group(0), rep_cell)
                    num_replaced += 1

            if num_replaced > 0:
                cand_v = self.work_dir / f"cand_pyals_cell_input_drop_dist{dist}.v"
                cand_v.write_text(mod_content, encoding="utf-8")
                candidates.append({
                    "name": f"pyALS_cell_input_drop_dist{dist}",
                    "path": cand_v,
                    "strategy": "pyals",
                    "distance": dist,
                    "replaced_luts": num_replaced,
                    "input_dropping": True,
                })

        # -------------------------------------------------------------
        # Phase 1.1 Action Item 2: 4-Bit Aligned Carry Truncation (CI = 1'b0)
        # -------------------------------------------------------------
        carry_ci_pat = re.compile(r"(\.CI\s*\(\s*)carry_lsb_\d+\[\d+\](\s*\))")
        if carry_ci_pat.search(content):
            trunc_content = carry_ci_pat.sub(r"\g<1>1'b0\g<2>", content)
            cand_ci_v = self.work_dir / "cand_pyals_nibble_trunc_4b.v"
            cand_ci_v.write_text(trunc_content, encoding="utf-8")
            candidates.append({
                "name": "pyALS_nibble_trunc_4b",
                "path": cand_ci_v,
                "strategy": "pyals",
                "distance": 4,
                "replaced_luts": 1,
                "carry_truncation": True,
            })
            logger.info("Generated Phase 1.1 Nibble-Aligned Carry Truncation candidate 'pyALS_nibble_trunc_4b'")

        # 1. Standard Distance-k rewrites (Pattern A)
        for dist in [1, 2, 3, 4]:
            mod_content = content
            num_replaced = 0

            for match in assign_matches:
                wire_name = match.group(1)
                width_bits = int(match.group(2))
                hex_str = match.group(3)
                inputs_str = match.group(4)

                bin_spec = bin(int(hex_str, 16))[2:].zfill(width_bits)[::-1]
                approx_lut = self.cache.get_lut_at_dist(bin_spec, dist)

                if approx_lut:
                    synth_spec = approx_lut[0][::-1]
                    approx_hex = f"{int(synth_spec, 2):0{max(1, width_bits // 4)}x}"
                    rep_assign = f"assign {wire_name} = {width_bits}'h{approx_hex} >> {{{inputs_str}}};"
                    mod_content = mod_content.replace(match.group(0), rep_assign)
                    num_replaced += 1

            if num_replaced > 0:
                cand_v = self.work_dir / f"cand_pyals_dist{dist}.v"
                cand_v.write_text(mod_content, encoding="utf-8")
                candidates.append({
                    "name": f"pyALS_dist{dist}",
                    "path": cand_v,
                    "strategy": "pyals",
                    "distance": dist,
                    "replaced_luts": num_replaced,
                })
                logger.info("Generated pyALS candidate 'pyALS_dist%d' with %d rewritten LUTs", dist, num_replaced)

        # 2. Process Pattern B: \$lut cell instances
        for dist in [1, 2, 3, 4]:
            mod_content = content
            num_replaced = 0

            for match in cell_matches:
                width_bits = int(match.group(1))
                bin_spec = match.group(2)[::-1]
                cell_name = match.group(4)
                conn_str = match.group(5)

                approx_lut = self.cache.get_lut_at_dist(bin_spec, dist)
                if approx_lut:
                    synth_spec = approx_lut[0][::-1]
                    rep_cell = f"\\$lut #( .LUT({width_bits}'b{synth_spec}), .WIDTH({width_bits}) ) {cell_name} ({conn_str});"
                    mod_content = mod_content.replace(match.group(0), rep_cell)
                    num_replaced += 1

            if num_replaced > 0:
                cand_v = self.work_dir / f"cand_pyals_cell_dist{dist}.v"
                cand_v.write_text(mod_content, encoding="utf-8")
                candidates.append({
                    "name": f"pyALS_cell_dist{dist}",
                    "path": cand_v,
                    "strategy": "pyals",
                    "distance": dist,
                    "replaced_luts": num_replaced,
                })

        # 3. Candidate: Zero/Pruning on lower-weight individual LUTs
        for i, match in enumerate(assign_matches[:min(len(assign_matches), 4)]):
            wire_name = match.group(1)
            width_bits = int(match.group(2))
            inputs_str = match.group(4)
            zero_hex = "0" * max(1, width_bits // 4)
            rep_zero = f"assign {wire_name} = {width_bits}'h{zero_hex} >> {{{inputs_str}}};"
            cand_zero_code = content.replace(match.group(0), rep_zero)
            cand_zero_v = self.work_dir / f"cand_pyals_prune_lut_{i}.v"
            cand_zero_v.write_text(cand_zero_code, encoding="utf-8")
            candidates.append({
                "name": f"pyALS_prune_lut_{i}",
                "path": cand_zero_v,
                "strategy": "pyals",
                "distance": 1,
                "replaced_luts": 1,
            })

        # 4. Candidate: Cumulative pruning of lower-weight LUTs
        cum_code = content
        for k in range(1, min(len(assign_matches) + 1, 9)):
            m = assign_matches[k - 1]
            w_name = m.group(1)
            w_bits = int(m.group(2))
            inp_str = m.group(4)
            z_hex = "0" * max(1, w_bits // 4)
            rep_z = f"assign {w_name} = {w_bits}'h{z_hex} >> {{{inp_str}}};"
            cum_code = cum_code.replace(m.group(0), rep_z)
            cand_cum_v = self.work_dir / f"cand_pyals_cum_prune_{k}.v"
            cand_cum_v.write_text(cum_code, encoding="utf-8")
            candidates.append({
                "name": f"pyALS_cum_prune_{k}",
                "path": cand_cum_v,
                "strategy": "pyals",
                "distance": k,
                "replaced_luts": k,
            })

        # 5. Candidate: Output bit-level pruning for multi-bit relaxed outputs
        rel_out = iface.get("relaxed_output")
        if rel_out:
            for k in [1, 2, 3, 4, 6, 8]:
                out_cand = content
                replaced = 0
                for b in range(k):
                    pat = rf"assign\s+{re.escape(rel_out)}\s*\[{b}\]\s*=[^;]+;"
                    if re.search(pat, out_cand):
                        rel_out_str = f"{rel_out} " if rel_out.startswith("\\") and not rel_out.endswith(" ") else rel_out
                        out_cand = re.sub(pat, lambda _, b=b, r=rel_out_str: f"assign {r}[{b}] = 1'b0;", out_cand)
                        replaced += 1
                if replaced > 0:
                    cand_out_v = self.work_dir / f"cand_pyals_out_bits_{k}.v"
                    cand_out_v.write_text(out_cand, encoding="utf-8")
                    candidates.append({
                        "name": f"pyALS_out_prune_{k}b",
                        "path": cand_out_v,
                        "strategy": "pyals",
                        "distance": k,
                        "replaced_luts": replaced,
                    })

        # 6. Candidate: Single-LUT6 4:2 and 5:3 / 5:2 compressor rewriting
        from axfpga.compressors import generate_compressor_candidates
        try:
            comp_cands = generate_compressor_candidates(
                cone_content=content,
                iface=iface,
                work_dir=self.work_dir,
            )
            candidates.extend(comp_cands)
        except Exception as exc:
            logger.warning("Could not generate compressor candidates: %s", exc)

        return candidates

    def optimize(
        self,
        metric: str = "MED",
        bound: float = 16.0,
        out_approx_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        """Execute pyALS catalog-based rewriting and select champion approximate mutant."""
        start_t = time.time()
        logger.info("=== Starting pyALS FPGA Approximate Logic Synthesis ===")
        logger.info("Cone: %s | Bound: %s=%.2f | Catalog: %s", self.cone_vlog, metric, bound, self.catalog_db)

        iface = self._parse_cone_interface()
        exact_clean_v = self.work_dir / "exact_cone_clean.v"
        mod_content = iface["raw_content"]

        # Guard: In extracted prefix adders (e.g. Kogge-Stone, Brent-Kung), bit 0 and MSB carry-out
        # are emitted on auxiliary ports (e.g. \U0.U0.U0.Po and \U0.U0.U64.Go) while the relaxed output
        # bus has unassigned floating bits at index 0 and index (width-1).
        # We tie those unassigned bits to the corresponding ports to ensure cycle-accurate simulation.
        rel_out = iface.get("relaxed_output")
        outputs = iface.get("outputs", {})
        if rel_out and rel_out in outputs:
            rel_info = outputs[rel_out]
            rel_w = rel_info["width"]
            rel_lsb = rel_info.get("lsb", 0)
            rel_msb = rel_info.get("msb", rel_w - 1)
            rel_fid = f"{rel_out} " if rel_out.startswith("\\") and not rel_out.endswith(" ") else rel_out
            clean_rel = rel_out.lower().lstrip("\\")
            for out_name, out_info in outputs.items():
                if out_name == rel_out:
                    continue
                clean_n = out_name.lower()
                out_fid = f"{out_name} " if out_name.startswith("\\") and not out_name.endswith(" ") else out_name
                if out_info["width"] == 1 and ("po" in clean_n or "p0" in clean_n):
                    if f"assign {rel_fid}[{rel_lsb}]" not in mod_content and f"assign {rel_out} [{rel_lsb}]" not in mod_content:
                        mod_content = mod_content.replace("endmodule", f"  assign {rel_fid}[{rel_lsb}] = {out_fid};\nendmodule")
                elif out_info["width"] == 1 and ("go" in clean_n or "g0" in clean_n or "cout" in clean_n or "co" in clean_n):
                    if f"assign {rel_fid}[{rel_msb}]" not in mod_content and f"assign {rel_out} [{rel_msb}]" not in mod_content:
                        mod_content = mod_content.replace("endmodule", f"  assign {rel_fid}[{rel_msb}] = {out_fid};\nendmodule")
                elif clean_n.endswith(f".{clean_rel}") or clean_n == clean_rel:
                    for b in range(rel_lsb, rel_msb + 1):
                        if f"assign {rel_fid}[{b}]" not in mod_content and f"assign {rel_out} [{b}]" not in mod_content:
                            mod_content = mod_content.replace("endmodule", f"  assign {rel_fid}[{b}] = {out_fid}[{b}];\nendmodule")
                        if f"assign {out_fid}[{b}]" not in mod_content and f"assign {out_name} [{b}]" not in mod_content:
                            mod_content = mod_content.replace("endmodule", f"  assign {out_fid}[{b}] = {rel_fid}[{b}];\nendmodule")
        exact_clean_v.write_text(mod_content, encoding="utf-8")

        base_res = self.estimator.evaluate_netlist(exact_clean_v, top_module=iface["module_name"])
        base_lcs = base_res.get("estimated_lcs", 100)
        base_lut = base_res.get("total_luts", 0)
        base_c4 = base_res.get("carry4", 0)

        # Generate catalog candidates
        candidates = self.generate_catalog_candidates(bound=bound)
        if not candidates:
            logger.warning("No pyALS catalog candidates generated; falling back to exact baseline")
            target_out = Path(out_approx_path) if out_approx_path else (self.work_dir / "approx_cone.v")
            shutil.copy(exact_clean_v, target_out)
            return {
                "status": "FALLBACK_BASELINE",
                "method": "exact_baseline",
                "strategy": "pyals",
                "baseline_lcs": base_lcs,
                "optimized_lcs": base_lcs,
                "lc_savings_pct": 0.0,
                "measured_error": 0.0,
                "output_file": str(target_out),
            }

        # Simulate & Evaluate candidates
        from axfpga.backend import FpgaBackend
        fb = FpgaBackend(cone_verilog_path=self.cone_vlog, work_dir=self.work_dir, sim_samples=self.sim_samples)
        stim_hex = fb._prepare_stimulus_dataset(iface, self.work_dir)

        valid_mutants = []
        for cand in candidates:
            sim_res = fb._simulate_candidate(exact_clean_v, cand["path"], iface, stim_hex, metric, self.work_dir)
            err_val = sim_res.get(metric.lower(), 1e9)
            if err_val <= bound:
                fpga_res = self.estimator.evaluate_netlist(cand["path"], top_module=iface["module_name"])
                cand_lcs = fpga_res.get("estimated_lcs", 9999)
                cand_lut = fpga_res.get("total_luts", 0)
                cand_c4 = fpga_res.get("carry4", 0)
                valid_mutants.append({
                    "name": cand["name"],
                    "strategy": "pyals",
                    "path": cand["path"],
                    "error": err_val,
                    "estimated_lcs": cand_lcs,
                    "total_luts": cand_lut,
                    "carry4": cand_c4,
                })

        beneficial_mutants = [m for m in valid_mutants if m["estimated_lcs"] < base_lcs or m["total_luts"] < base_lut]
        if beneficial_mutants:
            beneficial_mutants.sort(key=lambda m: (m["estimated_lcs"], m["total_luts"], m["error"]))
            champ = beneficial_mutants[0]
            champ_lcs = champ["estimated_lcs"]
            savings_pct = round(((base_lcs - champ_lcs) / base_lcs) * 100.0, 2) if base_lcs > 0 else 0.0
            logger.info("pyALS CHAMPION: %s | LCs %d -> %d (%.1f%% savings) | %s=%.2f",
                        champ["name"], base_lcs, champ_lcs, savings_pct, metric, champ["error"])
        else:
            logger.warning("No pyALS candidate achieved positive savings meeting bound %s=%.2f; falling back to exact baseline", metric, bound)
            champ = {
                "name": "baseline_fallback",
                "strategy": "pyals",
                "path": exact_clean_v,
                "error": 0.0,
                "estimated_lcs": base_lcs,
                "total_luts": base_lut,
                "carry4": base_c4,
            }
            savings_pct = 0.0

        # Phase 1.3 Action Item 2: Dedicated Xilinx 7-Series Yosys re-mapping pass
        remap_out = self.work_dir / "remapped_approx_cone.v"
        remap_cmd = (
            f"read_verilog -sv {champ['path']}; "
            f"hierarchy -check -auto-top; "
            f"opt_clean -purge; "
            f"opt -mux_undef -mux_bool -undriven -fine; "
            f"synth_xilinx -family {self.target_family} -noiopad -run begin:check; "
            f"opt_clean -purge; "
            f"write_verilog -noattr {remap_out}"
        )
        remap_res = subprocess.run(f"yosys -q -p \"{remap_cmd}\"", shell=True, executable="/bin/bash", capture_output=True, text=True)
        final_cand_path = remap_out if (remap_res.returncode == 0 and remap_out.exists()) else champ["path"]

        target_out = Path(out_approx_path) if out_approx_path else (self.work_dir / "approx_cone.v")
        shutil.copy(final_cand_path, target_out)

        # Generate Vivado sign-off batch synthesis script (Zybo / PYNQ / Artix-7)
        from axfpga.vivado import VivadoBatchRunner
        vivado_runner = VivadoBatchRunner(part=self.target_device or "xc7z010clg400-1")
        vivado_tcl = self.work_dir / "run_vivado_synth.tcl"
        vivado_runner.generate_tcl(
            top_module=iface["module_name"],
            verilog_files=[target_out],
            out_tcl_path=vivado_tcl,
        )

        elapsed = round(time.time() - start_t, 2)

        return {
            "status": "SUCCESS",
            "method": champ["name"],
            "strategy": "pyals",
            "target": "fpga",
            "family": self.target_family,
            "device": self.target_device,
            "baseline_lcs": base_lcs,
            "optimized_lcs": champ["estimated_lcs"],
            "lc_savings_pct": savings_pct,
            "exact_carry4": base_c4,
            "approx_carry4": champ["carry4"],
            "exact_luts": base_lut,
            "approx_luts": champ["total_luts"],
            "measured_error": champ["error"],
            "error_metric": metric,
            "bound": bound,
            "output_file": str(target_out),
            "vivado_tcl": str(vivado_tcl),
            "elapsed_seconds": elapsed,
        }
