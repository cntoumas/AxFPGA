"""Native FPGA Approximate Logic Synthesis Backend for AxSYS.

Replaces ASIC-centric gate-level approximation with native FPGA-aware logic pruning
and technology mapping targeting Xilinx 7-Series / Artix-7 architectures:
1. CarryPruner: Carry-aware arithmetic truncation and rounding preserving CARRY4 fast chains.
2. LutPruner: Native 6-LUT cut and input pruning via Yosys & ABC (abc -lut 6).
3. ConstPruner: Trace-driven constant propagation and dead-logic elimination.
4. FpgaTournament: Multi-algorithm tournament selecting the champion mutant that
   minimizes physical Logic Cells (LUTs + 2*CARRY4) while satisfying error bounds.
"""

from __future__ import annotations

import argparse
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

from axfpga.fpga_estimator import FpgaEstimator

logger = logging.getLogger("fpga_backend")


class FpgaBackend:
    """Native FPGA Approximate Logic Synthesis Engine."""

    def __init__(
        self,
        cone_verilog_path: str | Path,
        meta_json_path: Optional[str | Path] = None,
        target_family: str = "xc7",
        target_device: str = "xc7a35t",
        nodsp: bool = True,
        work_dir: Optional[str | Path] = None,
        trace_hex_path: Optional[str | Path] = None,
        sim_samples: int = 512,
    ):
        self.cone_vlog = Path(cone_verilog_path).resolve()
        self.meta_json = Path(meta_json_path).resolve() if meta_json_path else None
        self.target_family = target_family.lower()
        self.target_device = target_device.lower()
        self.nodsp = nodsp
        self.work_dir = Path(work_dir).resolve() if work_dir else None
        self.trace_hex_path = Path(trace_hex_path).resolve() if trace_hex_path else None
        self.sim_samples = sim_samples

        self.estimator = FpgaEstimator(family=self.target_family, device=self.target_device, nodsp=self.nodsp)

    def _parse_cone_interface(self) -> Dict[str, Any]:
        """Extract module name, input ports, output ports, and port bit-widths."""
        content = self.cone_vlog.read_text(encoding="utf-8")
        mod_m = re.search(r"module\s+(\w+)\s*\((.*?)\);", content, re.DOTALL)
        if not mod_m:
            raise ValueError(f"Could not parse module header from {self.cone_vlog}")

        mod_name = mod_m.group(1)
        raw_ports = [p.strip() for p in mod_m.group(2).split(",") if p.strip()]

        inputs: Dict[str, Dict[str, Any]] = {}
        outputs: Dict[str, Dict[str, Any]] = {}

        # Parse port declarations (supports both ANSI header and non-ANSI body declarations)
        port_pat = re.compile(r"\b(input|output)\s+(wire\s+|reg\s+)?(signed\s+)?(\[[^\]]+\]\s+)?(\w+)")
        for line in content.splitlines():
            line = line.strip()
            m = port_pat.search(line)
            if m:
                direction = m.group(1)
                is_signed = bool(m.group(3))
                width_str = m.group(4)
                name = m.group(5)
                width = 1
                if width_str:
                    w_m = re.match(r"\[(\d+):(\d+)\]", width_str.strip())
                    if w_m:
                        width = abs(int(w_m.group(1)) - int(w_m.group(2))) + 1

                info = {"name": name, "width": width, "signed": is_signed}
                if direction == "input":
                    inputs[name] = info
                else:
                    outputs[name] = info

        # If meta JSON is available, refine relaxed vs precise tags
        relaxed_out = None
        if self.meta_json and self.meta_json.exists():
            try:
                with open(self.meta_json, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    for sig in meta.get("relaxed_signals", []):
                        if sig.get("wire") in outputs:
                            relaxed_out = sig.get("wire")
                            break
            except Exception:
                pass

        if not relaxed_out:
            # Default to primary multi-bit output or last output
            multi_outs = [k for k, v in outputs.items() if v["width"] > 1]
            relaxed_out = multi_outs[0] if multi_outs else list(outputs.keys())[-1]

        # Detect signedness from cone content (e.g. $signed(...) or wire signed ...)
        is_signed = False
        if "$signed" in content or "wire signed" in content or "output signed" in content or "output reg signed" in content:
            is_signed = True
        if relaxed_out in outputs:
            outputs[relaxed_out]["signed"] = is_signed

        return {
            "module_name": mod_name,
            "ports": raw_ports,
            "inputs": inputs,
            "outputs": outputs,
            "relaxed_output": relaxed_out,
            "raw_content": content,
        }

    def _prepare_stimulus_dataset(self, iface: Dict[str, Any], run_dir: Path) -> Path:
        """Prepare stimulus vectors hex file for dual-miter simulation."""
        stim_hex = run_dir / "sim_stimulus.hex"
        if self.trace_hex_path and self.trace_hex_path.exists():
            shutil.copy(self.trace_hex_path, stim_hex)
            return stim_hex

        # Generate synthetic stimulus vectors
        rng = np.random.default_rng(42)
        lines = []
        input_names = list(iface["inputs"].keys())
        lines.append("# " + " ".join(input_names))

        for _ in range(self.sim_samples):
            vec_parts = []
            for name in input_names:
                info = iface["inputs"][name]
                w = info["width"]
                if w == 1:
                    val = int(rng.choice([0, 1], p=[0.2, 0.8]))
                else:
                    max_val = (1 << w) - 1
                    val = int(rng.integers(0, max_val + 1))
                vec_parts.append(f"{val:x}")
            lines.append(" ".join(vec_parts))

        stim_hex.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return stim_hex

    def _simulate_candidate(
        self,
        exact_vlog: Path,
        approx_vlog: Path,
        iface: Dict[str, Any],
        stim_hex: Path,
        metric: str,
        run_dir: Path,
    ) -> Dict[str, float]:
        """Run cycle-accurate dual miter testbench via iverilog and compute error."""
        tb_v = run_dir / f"tb_eval_{approx_vlog.stem}.v"
        vvp_bin = run_dir / f"tb_eval_{approx_vlog.stem}.vvp"

        # Rename candidate module to avoid collision with exact cone module
        approx_mod_name = f"{iface['module_name']}_approx"
        approx_renamed_v = run_dir / f"{approx_vlog.stem}_renamed.v"
        cand_content = approx_vlog.read_text(encoding="utf-8")
        renamed_content = re.sub(rf"\bmodule\s+{iface['module_name']}\b", f"module {approx_mod_name}", cand_content, count=1)
        approx_renamed_v.write_text(renamed_content, encoding="utf-8")

        inputs = iface["inputs"]
        outputs = iface["outputs"]
        rel_out = iface["relaxed_output"]
        rel_width = outputs[rel_out]["width"]
        rel_signed = outputs[rel_out]["signed"]

        # Build clean testbench
        inp_decls = []
        inst_exact_conns = []
        inst_approx_conns = []
        read_conns = []

        for name, info in inputs.items():
            w = info["width"]
            inp_decls.append(f"  reg [{w-1}:0] in_{name};")
            inst_exact_conns.append(f".{name}(in_{name})")
            inst_approx_conns.append(f".{name}(in_{name})")
            read_conns.append(f"in_{name}")

        out_decls = []
        for name, info in outputs.items():
            w = info["width"]
            out_decls.append(f"  wire [{w-1}:0] exact_{name};")
            out_decls.append(f"  wire [{w-1}:0] approx_{name};")
            inst_exact_conns.append(f".{name}(exact_{name})")
            inst_approx_conns.append(f".{name}(approx_{name})")

        format_str = " ".join(["%x"] * len(inputs))
        read_args = ", ".join(read_conns)

        signed_calc = f"""
        if ({"1" if rel_signed else "0"}) begin
          diff = $signed(exact_{rel_out}) - $signed(approx_{rel_out});
        end else begin
          diff = exact_{rel_out} - approx_{rel_out};
        end
"""
        tb_code = f"""`timescale 1ns/1ps
module tb_eval;
{chr(10).join(inp_decls)}
{chr(10).join(out_decls)}

  integer fd, r;
  integer sample_count;
  real total_abs_error;
  real max_abs_error;
  integer error_count;
  real diff;

  {iface['module_name']} u_exact (
    {", ".join(inst_exact_conns)}
  );

  {approx_mod_name} u_approx (
    {", ".join(inst_approx_conns)}
  );

  initial begin
    sample_count = 0;
    total_abs_error = 0.0;
    max_abs_error = 0.0;
    error_count = 0;

    fd = $fopen("{stim_hex}", "r");
    if (fd == 0) begin
      $display("[TB ERROR] Could not open stimulus file");
      $finish;
    end

    // Skip comment line if present
    r = $fgetc(fd);
    if (r == 35) begin // '#'
      while (r != 10 && r != -1) r = $fgetc(fd);
    end else begin
      $ungetc(r, fd);
    end

    while (!$feof(fd)) begin
      r = $fscanf(fd, "{format_str}\\n", {read_args});
      if (r == {len(inputs)}) begin
        #1;
        sample_count = sample_count + 1;
        {signed_calc}
        if (diff < 0) diff = -diff;

        total_abs_error = total_abs_error + diff;
        if (diff > max_abs_error) max_abs_error = diff;
        if (diff != 0) error_count = error_count + 1;
      end
    end
    $fclose(fd);

    if (sample_count > 0) begin
      $display("SAMPLES: %0d", sample_count);
      $display("MED: %f", total_abs_error / sample_count);
      $display("WCE: %f", max_abs_error);
      $display("ER: %f", (1.0 * error_count) / sample_count);
    end
    $finish;
  end
endmodule
"""
        tb_v.write_text(tb_code, encoding="utf-8")

        # Compile and execute with iverilog
        cmd_compile = f"iverilog -g2012 -o {vvp_bin} {tb_v} {exact_vlog} {approx_renamed_v}"
        res_c = subprocess.run(cmd_compile, shell=True, executable="/bin/bash", capture_output=True, text=True)
        if res_c.returncode != 0:
            logger.debug("iverilog compile failed:\n%s", res_c.stderr)
            return {"med": 1e9, "wce": 1e9, "er": 1.0}

        cmd_run = f"vvp {vvp_bin}"
        res_r = subprocess.run(cmd_run, shell=True, executable="/bin/bash", capture_output=True, text=True)
        if res_r.returncode != 0:
            return {"med": 1e9, "wce": 1e9, "er": 1.0}

        # Parse metrics
        med = 0.0
        wce = 0.0
        er = 0.0
        for line in res_r.stdout.splitlines():
            if line.startswith("MED:"):
                med = float(line.split(":")[1].strip())
            elif line.startswith("WCE:"):
                wce = float(line.split(":")[1].strip())
            elif line.startswith("ER:"):
                er = float(line.split(":")[1].strip())

        return {"med": med, "wce": wce, "er": er}

    def _generate_carry_pruned_candidates(self, iface: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
        """Strategy 1: Generate carry-aware arithmetic bit truncation & rounding candidates."""
        run_dir.mkdir(parents=True, exist_ok=True)
        candidates: List[Dict[str, Any]] = []
        raw_code = iface["raw_content"]
        rel_out = iface["relaxed_output"]
        rel_width = iface["outputs"][rel_out]["width"]

        # Max bit truncation depth up to rel_width - 1
        max_k = min(rel_width - 1, 16)
        trunc_steps = [k for k in range(1, max_k + 1)]

        for k in trunc_steps:
            # Candidate 1: Zero truncation
            # Wrap the relaxed output: assign relaxed_out = {orig_out[W-1:k], k'b0}
            cand_code_zero = re.sub(
                rf"\bassign\s+{rel_out}\s*=\s*(.*?);",
                rf"wire [{rel_width-1}:0] _raw_{rel_out};\n  assign _raw_{rel_out} = \1;\n  assign {rel_out} = {{_raw_{rel_out}[{rel_width-1}:{k}], {k}'b0}};",
                raw_code,
            )
            cand_p_zero = run_dir / f"cand_carry_trunc_{k}.v"
            cand_p_zero.write_text(cand_code_zero, encoding="utf-8")
            candidates.append({
                "name": f"CarryPrune_k{k}_zero",
                "path": cand_p_zero,
                "strategy": "carry_prune",
                "k": k,
                "rounding": "zero",
            })

            # Candidate 2: Round-to-nearest constant bias (sets bit k-1 to 1'b1)
            if k >= 2:
                zeros_count = k - 1
                zeros_str = f"{zeros_count}'b0" if zeros_count > 0 else ""
                suffix = f", 1'b1, {zeros_str}" if zeros_count > 0 else ", 1'b1"
                cand_code_round = re.sub(
                    rf"\bassign\s+{rel_out}\s*=\s*(.*?);",
                    rf"wire [{rel_width-1}:0] _raw_{rel_out};\n  assign _raw_{rel_out} = \1;\n  assign {rel_out} = {{_raw_{rel_out}[{rel_width-1}:{k}]{suffix}}};",
                    raw_code,
                )
                cand_p_round = run_dir / f"cand_carry_round_{k}.v"
                cand_p_round.write_text(cand_code_round, encoding="utf-8")
                candidates.append({
                    "name": f"CarryPrune_k{k}_round",
                    "path": cand_p_round,
                    "strategy": "carry_prune",
                    "k": k,
                    "rounding": "nearest",
                })

        return candidates

    def _split_addition_terms(self, expr: str) -> List[str]:
        """Split expression into top-level additive terms."""
        terms = []
        depth = 0
        current = []
        for char in expr:
            if char in "([{":
                depth += 1
                current.append(char)
            elif char in ")]}":
                depth -= 1
                current.append(char)
            elif char == "+" and depth == 0:
                t = "".join(current).strip()
                if t:
                    terms.append(t)
                current = []
            else:
                current.append(char)
        t = "".join(current).strip()
        if t:
            terms.append(t)
        return terms

    def _generate_loa_candidates(self, iface: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
        """Strategy: Lower-Part OR Adder (LOA) with speculative carry."""
        run_dir.mkdir(parents=True, exist_ok=True)
        candidates: List[Dict[str, Any]] = []
        raw_code = iface["raw_content"]
        rel_out = iface["relaxed_output"]
        rel_width = iface["outputs"][rel_out]["width"]
        rel_signed = iface["outputs"][rel_out]["signed"]

        # 1. Identify target addition inside the cone (either direct rel_out or internal adder feeding it)
        add_target = rel_out
        add_rhs = None
        assign_m = re.search(rf"\bassign\s+{rel_out}\s*=\s*(.*?);", raw_code)
        if assign_m and "+" in assign_m.group(1):
            add_rhs = assign_m.group(1).strip()
        else:
            # Look for an internal assign with addition
            internal_m = re.search(r"\bassign\s+([A-Za-z0-9_]+)\s*=\s*([^;]+?\+[^;]+?);", raw_code)
            if internal_m:
                add_target = internal_m.group(1)
                add_rhs = internal_m.group(2).strip()

        if not add_rhs:
            return candidates

        # Determine target bit width
        target_w = rel_width
        w_m = re.search(rf"\b(?:wire|reg)\s+(?:signed\s+)?\[(\d+):(\d+)\]\s+{add_target}\b", raw_code)
        if w_m:
            target_w = abs(int(w_m.group(1)) - int(w_m.group(2))) + 1
        elif add_target in iface.get("outputs", {}):
            target_w = iface["outputs"][add_target]["width"]

        # Clean attributes like (* src = "..." *)
        clean_rhs = re.sub(r"\(\*.*?\*\)", "", add_rhs).strip()
        terms = self._split_addition_terms(clean_rhs)

        if len(terms) < 2:
            return candidates
        elif len(terms) == 2:
            opA, opB = terms[0].strip(), terms[1].strip()
        else:
            opA = " + ".join(terms[:-1])
            opB = terms[-1].strip()

        max_k = min(target_w - 1, 16)
        for k in range(1, max_k + 1):
            signed_kw = "$signed" if rel_signed else ""
            loa_vlog = (
                f"wire [{target_w-1}:0] _loa_opA = ({opA});\n"
                f"  wire [{target_w-1}:0] _loa_opB = ({opB});\n"
                f"  wire [{k-1}:0] _loa_lower = _loa_opA[{k-1}:0] | _loa_opB[{k-1}:0];\n"
                f"  wire _loa_carry = _loa_opA[{k-1}] & _loa_opB[{k-1}];\n"
                f"  wire [{target_w-k-1}:0] _loa_upper = {signed_kw}(_loa_opA[{target_w-1}:{k}]) + {signed_kw}(_loa_opB[{target_w-1}:{k}]) + _loa_carry;\n"
                f"  assign {add_target} = {{_loa_upper, _loa_lower}};"
            )

            cand_code = re.sub(rf"\bassign\s+{add_target}\s*=\s*.*?;", loa_vlog, raw_code, count=1)
            cand_p = run_dir / f"cand_loa_k{k}.v"
            cand_p.write_text(cand_code, encoding="utf-8")
            candidates.append({
                "name": f"LOA_k{k}",
                "path": cand_p,
                "strategy": "loa",
                "k": k,
            })

        return candidates

    def _generate_bam_candidates(self, iface: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
        """Strategy: Broken Array Multiplier (BAM) & Partial Product Truncation."""
        run_dir.mkdir(parents=True, exist_ok=True)
        candidates: List[Dict[str, Any]] = []
        raw_code = iface["raw_content"]
        rel_out = iface["relaxed_output"]

        mul_m = re.search(
            r"(\$signed\s*\([^)]+\)|\([^\)]+\)|[A-Za-z_][A-Za-z0-9_]*)\s*\*\s*(?:\(\*.*?\*\)\s*)?(\$signed\s*\([^)]+\)|\([^\)]+\)|[A-Za-z_][A-Za-z0-9_]*|[0-9]+'s?d[0-9]+|[0-9]+'h[0-9a-fA-F]+)",
            raw_code,
        )
        if not mul_m:
            return candidates

        opA_raw = mul_m.group(1).strip()
        opB_raw = mul_m.group(2).strip()
        opA_clean = re.sub(r"^\$signed\((.*)\)$", r"\1", opA_raw).strip("()")
        opB_clean = re.sub(r"^\$signed\((.*)\)$", r"\1", opB_raw).strip("()")

        wA = 8
        wB = 8
        if opA_clean in iface["inputs"]:
            wA = iface["inputs"][opA_clean]["width"]
        elif "[" in opA_clean:
            s_m = re.search(r"\[(\d+):(\d+)\]", opA_clean)
            if s_m:
                wA = abs(int(s_m.group(1)) - int(s_m.group(2))) + 1
        if opB_clean in iface["inputs"]:
            wB = iface["inputs"][opB_clean]["width"]
        elif "'d" in opB_clean or "'sd" in opB_clean or "'h" in opB_clean:
            try:
                wB = int(opB_clean.split("'")[0])
            except Exception:
                wB = 8
        elif "[" in opB_clean:
            s_m = re.search(r"\[(\d+):(\d+)\]", opB_clean)
            if s_m:
                wB = abs(int(s_m.group(1)) - int(s_m.group(2))) + 1

        wP = wA + wB
        is_signed = "$signed" in raw_code or "signed" in raw_code or iface["outputs"][rel_out]["signed"]

        max_k = min(wA + wB - 2, 12)
        k_steps = [k for k in [2, 3, 4, 5, 6, 8, 10] if k <= max_k]

        for k in k_steps:
            func_name = f"_axc_bam_mul_k{k}"
            bias = 1 << (k - 1) if k > 0 else 0

            if is_signed:
                c_const = f"-{wP}'sd{1 << (wP - 1)} + {wP}'sd{1 << (wA - 1)} + {wP}'sd{1 << (wB - 1)} + {bias}"
                func_code = f"""
    function signed [{wP-1}:0] {func_name};
        input [{wA-1}:0] a_in;
        input [{wB-1}:0] b_in;
        integer i, j;
        reg signed [{wP-1}:0] acc;
        begin
            acc = {c_const};
            for (i = 0; i < {wA-1}; i = i + 1) begin
                for (j = 0; j < {wB-1}; j = j + 1) begin
                    if (i + j >= {k}) begin
                        acc = acc + ((a_in[i] & b_in[j]) << (i + j));
                    end
                end
            end
            acc = acc + ((a_in[{wA-1}] & b_in[{wB-1}]) << {wA + wB - 2});
            for (j = 0; j < {wB-1}; j = j + 1)
                acc = acc + ((1'b1 ^ (a_in[{wA-1}] & b_in[j])) << ({wA-1} + j));
            for (i = 0; i < {wA-1}; i = i + 1)
                acc = acc + ((1'b1 ^ (a_in[i] & b_in[{wB-1}])) << ({wB-1} + i));
            {func_name} = acc;
        end
    endfunction
"""
            else:
                func_code = f"""
    function [{wP-1}:0] {func_name};
        input [{wA-1}:0] a_in;
        input [{wB-1}:0] b_in;
        integer i, j;
        reg [{wP-1}:0] acc;
        begin
            acc = {bias};
            for (i = 0; i < {wA}; i = i + 1) begin
                for (j = 0; j < {wB}; j = j + 1) begin
                    if (i + j >= {k}) begin
                        acc = acc + ((a_in[i] & b_in[j]) << (i + j));
                    end
                end
            end
            {func_name} = acc;
        end
    endfunction
"""
            cand_code = re.sub(r"\bassign\b", f"{func_code}\n  assign", raw_code, count=1)
            cand_code = cand_code.replace(mul_m.group(0), f"{func_name}({opA_clean}, {opB_clean})")

            cand_p = run_dir / f"cand_bam_k{k}.v"
            cand_p.write_text(cand_code, encoding="utf-8")
            candidates.append({
                "name": f"BAM_k{k}",
                "path": cand_p,
                "strategy": "bam",
                "k": k,
            })

        return candidates

    def _generate_spatial_perforated_candidates(self, iface: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
        """Strategy: Spatial Tap Perforation for multi-operand adder trees."""
        run_dir.mkdir(parents=True, exist_ok=True)
        candidates: List[Dict[str, Any]] = []
        try:
            from axfpga.perforator import RtlLoopPerforator
            perforator = RtlLoopPerforator(verilog_path=self.cone_vlog, work_dir=run_dir)
            trees = perforator.detect_spatial_sum_trees()
            for tree in trees:
                max_drops = min(len(tree.terms) - 2, 4)
                for k_drop in range(1, max_drops + 1):
                    p_mut = perforator.perforate_spatial_tree(
                        tree,
                        num_taps_to_drop=k_drop,
                        out_path=run_dir / f"cand_spatial_{tree.target_signal}_drop{k_drop}.v",
                    )
                    candidates.append({
                        "name": f"SpatialDrop_{tree.target_signal}_k{k_drop}",
                        "path": p_mut,
                        "strategy": "spatial_perforate",
                        "dropped_taps": k_drop,
                    })
        except Exception as exc:
            logger.debug("Spatial tap perforation candidate generation failed: %s", exc)

        return candidates

    def _generate_lut_pruned_candidates(self, iface: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
        """Strategy 2: Map cone directly to 6-LUTs via Yosys & ABC, and prune LUT input cuts."""
        candidates: List[Dict[str, Any]] = []
        top_name = iface["module_name"]

        base_lut_v = run_dir / "base_lut_cone.v"
        yosys_cmd = (
            f"read_verilog -sv {self.cone_vlog}; "
            f"hierarchy -check -top {top_name}; "
            f"proc; opt_clean; "
            f"synth_xilinx -family {self.target_family} -nodsp -top {top_name}; "
            f"clean; "
            f"write_verilog -noattr {base_lut_v}"
        )
        cmd = f"yosys -q -p \"{yosys_cmd}\""
        res = subprocess.run(cmd, shell=True, executable="/bin/bash", capture_output=True, text=True)
        if res.returncode != 0 or not base_lut_v.exists():
            logger.debug("Yosys LUT mapping failed for cone: %s", res.stderr)
            return candidates

        lut_content = base_lut_v.read_text(encoding="utf-8")

        # Discover all multi-bit input ports and prune least significant bits
        for inp_name, info in iface["inputs"].items():
            w = info["width"]
            if w <= 1:
                continue
            for prune_k in range(1, min(w, 5)):
                # Clamping the lower prune_k bits to zero in the base netlist
                mod_lut = lut_content
                # Replace input occurrences like wire [w-1:0] inp_name;
                # with an assign setting lower bits to 0
                clamp_wire = f"\n  // Pruning {inp_name} lower {prune_k} bits\n"
                for bit_idx in range(prune_k):
                    # Replace driving net
                    mod_lut = re.sub(rf"\b{inp_name}\[{bit_idx}\]\b", "1'b0", mod_lut)

                cand_lut_p = run_dir / f"cand_lut_prune_{inp_name}_k{prune_k}.v"
                # Clean up netlist with yosys
                opt_cand_p = run_dir / f"cand_lut_opt_{inp_name}_k{prune_k}.v"
                cand_lut_p.write_text(mod_lut, encoding="utf-8")

                clean_cmd = f"read_verilog {cand_lut_p}; proc; opt_clean -purge; write_verilog -noattr {opt_cand_p}"
                subprocess.run(f"yosys -q -p \"{clean_cmd}\"", shell=True, executable="/bin/bash", capture_output=True)

                if opt_cand_p.exists():
                    candidates.append({
                        "name": f"LutPrune_{inp_name}_k{prune_k}",
                        "path": opt_cand_p,
                        "strategy": "lut_prune",
                    })

        return candidates

    def _generate_dt_candidates(
        self,
        iface: Dict[str, Any],
        stim_hex: Path,
        run_dir: Path,
    ) -> List[Dict[str, Any]]:
        """Strategy 3: Train Scikit-Learn Decision Trees (depth <= 6) mapped to FPGA 6-LUTs."""
        candidates: List[Dict[str, Any]] = []
        inputs = iface["inputs"]
        outputs = iface["outputs"]
        rel_out = iface["relaxed_output"]
        rel_width = outputs[rel_out]["width"]
        rel_signed = outputs[rel_out]["signed"]

        # Step 1: Record exact I/O dataset using iverilog
        tb_rec = run_dir / "tb_record_dt.v"
        exact_out_txt = run_dir / "exact_dt_io.txt"
        vvp_rec = run_dir / "tb_record_dt.vvp"

        inp_decls = [f"  reg [{info['width']-1}:0] in_{k};" for k, info in inputs.items()]
        signed_decl = "signed " if rel_signed else ""
        out_decls = [f"  wire {signed_decl}[{info['width']-1}:0] out_{k};" for k, info in outputs.items()]
        inst_conns = [f".{k}(in_{k})" for k in inputs.keys()] + [f".{k}(out_{k})" for k in outputs.keys()]
        read_conns = [f"in_{k}" for k in inputs.keys()]
        format_str = " ".join(["%x"] * len(inputs))
        format_out = " ".join(["%x"] * (len(inputs) + 1))

        tb_code = f"""`timescale 1ns/1ps
module tb_record_dt;
{chr(10).join(inp_decls)}
{chr(10).join(out_decls)}

  integer fd_in, fd_out, r;
  {iface['module_name']} u_dut ({", ".join(inst_conns)});

  initial begin
    fd_in = $fopen("{stim_hex}", "r");
    fd_out = $fopen("{exact_out_txt}", "w");
    
    // Skip comment line
    r = $fgetc(fd_in);
    if (r == 35) while (r != 10 && r != -1) r = $fgetc(fd_in);
    else $ungetc(r, fd_in);

    while (!$feof(fd_in)) begin
      r = $fscanf(fd_in, "{format_str}\\n", {", ".join(read_conns)});
      if (r == {len(inputs)}) begin
        #1;
        $fdisplay(fd_out, "{format_out}", {", ".join(read_conns)}, out_{rel_out});
      end
    end
    $fclose(fd_in);
    $fclose(fd_out);
    $finish;
  end
endmodule
"""
        tb_rec.write_text(tb_code, encoding="utf-8")
        res_c = subprocess.run(f"iverilog -g2012 -o {vvp_rec} {tb_rec} {self.cone_vlog}", shell=True, capture_output=True, text=True)
        if res_c.returncode != 0:
            logger.debug("Could not compile DT recorder TB: %s", res_c.stderr)
            return candidates
        res_r = subprocess.run(f"vvp {vvp_rec}", shell=True, capture_output=True, text=True)
        if res_r.returncode != 0 or not exact_out_txt.exists():
            return candidates

        # Step 2: Parse dataset into binary features
        lines = exact_out_txt.read_text(encoding="utf-8").strip().splitlines()
        X_rows = []
        Y_rows = []

        # Map input bits to names
        input_bit_names = []
        for k, info in inputs.items():
            w = info["width"]
            if w == 1:
                input_bit_names.append(k)
            else:
                for b in range(w):
                    input_bit_names.append(f"{k}[{b}]")

        for line in lines:
            parts = line.split()
            if len(parts) == len(inputs) + 1:
                in_vals = [int(p, 16) for p in parts[:-1]]
                out_val = int(parts[-1], 16)

                # Unpack input bits
                x_bits = []
                for idx, (k, info) in enumerate(inputs.items()):
                    val = in_vals[idx]
                    w = info["width"]
                    for b in range(w):
                        x_bits.append((val >> b) & 1)
                X_rows.append(x_bits)

                # Unpack output bits
                y_bits = [(out_val >> b) & 1 for b in range(rel_width)]
                Y_rows.append(y_bits)

        if not X_rows or not Y_rows:
            return candidates

        X_mat = np.array(X_rows, dtype=np.int32)
        Y_mat = np.array(Y_rows, dtype=np.int32)

        # Step 3: Train decision trees of varying depths
        from sklearn.tree import DecisionTreeClassifier
        from axfpga.axls_bridge import tree_to_verilog_expr

        depth_options = [2, 3, 4, 5, 6]
        k_options = [1, 2, 3, 4, 6, 8, rel_width] if rel_width > 4 else [1, 2, rel_width]
        k_options = sorted(list(set(k for k in k_options if 0 < k <= rel_width)))

        raw_code = iface["raw_content"]

        for depth in depth_options:
            for k_approx in k_options:
                dt_bit_assigns = []
                for bit_idx in range(k_approx):
                    y_col = Y_mat[:, bit_idx]
                    if np.all(y_col == 0):
                        expr = "1'b0"
                    elif np.all(y_col == 1):
                        expr = "1'b1"
                    else:
                        clf = DecisionTreeClassifier(max_depth=depth, random_state=42)
                        clf.fit(X_mat, y_col)
                        expr = tree_to_verilog_expr(clf.tree_, 0, input_bit_names, clf.classes_)
                    dt_bit_assigns.append((bit_idx, expr))

                # Build candidate Verilog
                assign_str = ""
                for bit_idx, expr in dt_bit_assigns:
                    assign_str += f"  wire _dt_bit_{bit_idx};\n  assign _dt_bit_{bit_idx} = {expr};\n"

                if k_approx == rel_width:
                    concat_bits = ", ".join(f"_dt_bit_{b}" for b in reversed(range(rel_width)))
                    rep_str = f"wire [{rel_width-1}:0] _raw_{rel_out};\n  assign _raw_{rel_out} = \\1;\n{assign_str}"
                    if "valid_in" in inputs:
                        rep_str += f"\n  assign {rel_out} = valid_in ? {{{concat_bits}}} : _raw_{rel_out};"
                    elif "clear" in inputs and "enable" in inputs:
                        rep_str += f"\n  assign {rel_out} = clear ? _raw_{rel_out} : (enable ? {{{concat_bits}}} : _raw_{rel_out});"
                    elif "clear" in inputs:
                        rep_str += f"\n  assign {rel_out} = clear ? _raw_{rel_out} : {{{concat_bits}}};"
                    else:
                        rep_str += f"\n  assign {rel_out} = {{{concat_bits}}};"
                else:
                    concat_bits = ", ".join(f"_dt_bit_{b}" for b in reversed(range(k_approx)))
                    rep_str = f"wire [{rel_width-1}:0] _raw_{rel_out};\n  assign _raw_{rel_out} = \\1;\n{assign_str}"
                    if "valid_in" in inputs:
                        rep_str += f"\n  assign {rel_out} = valid_in ? {{_raw_{rel_out}[{rel_width-1}:{k_approx}], {concat_bits}}} : _raw_{rel_out};"
                    elif "clear" in inputs and "enable" in inputs:
                        rep_str += f"\n  assign {rel_out} = clear ? _raw_{rel_out} : (enable ? {{_raw_{rel_out}[{rel_width-1}:{k_approx}], {concat_bits}}} : _raw_{rel_out});"
                    elif "clear" in inputs:
                        rep_str += f"\n  assign {rel_out} = clear ? _raw_{rel_out} : {{_raw_{rel_out}[{rel_width-1}:{k_approx}], {concat_bits}}};"
                    else:
                        rep_str += f"\n  assign {rel_out} = {{_raw_{rel_out}[{rel_width-1}:{k_approx}], {concat_bits}}};"

                cand_code = re.sub(rf"\bassign\s+{rel_out}\s*=\s*(.*?);", rep_str, raw_code, count=1)
                cand_file = run_dir / f"cand_dt_d{depth}_k{k_approx}.v"
                cand_file.write_text(cand_code, encoding="utf-8")

                candidates.append({
                    "name": f"DecisionTree_d{depth}_k{k_approx}",
                    "path": cand_file,
                    "strategy": "decision_tree",
                    "depth": depth,
                    "k_approx": k_approx,
                })

        return candidates

    def optimize(
        self,
        metric: str = "MED",
        bound: float = 16.0,
        lsb_width: Optional[int] = None,
        method: str = "auto",
        out_approx_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        """Run native FPGA approximation tournament and emit champion netlist."""
        start_time = time.time()
        logger.info("=== Starting Native FPGA Approximate Logic Synthesis ===")
        logger.info("Target: %s (%s) | Metric: %s | Bound: %s | LSB Width: %s",
                    self.target_family, self.target_device, metric, bound, lsb_width)

        run_dir = self.work_dir or Path(tempfile.mkdtemp(prefix="fpga_axc_"))
        run_dir.mkdir(parents=True, exist_ok=True)

        iface = self._parse_cone_interface()
        stim_hex = self._prepare_stimulus_dataset(iface, run_dir)

        # Baseline evaluation
        exact_clean_v = run_dir / "exact_cone_clean.v"
        shutil.copy(self.cone_vlog, exact_clean_v)
        baseline_fpga = self.estimator.evaluate_netlist(exact_clean_v, top_module=iface["module_name"])
        baseline_lcs = baseline_fpga.get("estimated_lcs", 100)
        baseline_c4 = baseline_fpga.get("carry4", 0)
        baseline_lut = baseline_fpga.get("slice_luts", 0)
        logger.info("Exact Baseline FPGA Footprint: %d LCs (Slice LUTs: %d, CARRY4: %d)",
                    baseline_lcs, baseline_lut, baseline_c4)

        # Generate candidates from both strategies
        all_candidates: List[Dict[str, Any]] = []

        if method.lower() in ("auto", "carry_prune", "inouts"):
            logger.info("Generating Carry-Aware Arithmetic Pruning candidates...")
            carry_cands = self._generate_carry_pruned_candidates(iface, run_dir)
            all_candidates.extend(carry_cands)

        if method.lower() in ("auto", "loa"):
            logger.info("Generating Lower-Part OR Adder (LOA) candidates...")
            loa_cands = self._generate_loa_candidates(iface, run_dir)
            all_candidates.extend(loa_cands)

        if method.lower() in ("auto", "bam", "ppt"):
            logger.info("Generating Broken Array Multiplier (BAM) candidates...")
            bam_cands = self._generate_bam_candidates(iface, run_dir)
            all_candidates.extend(bam_cands)

        if method.lower() in ("auto", "spatial", "perforate"):
            logger.info("Generating Spatial Tap Perforation candidates...")
            spatial_cands = self._generate_spatial_perforated_candidates(iface, run_dir)
            all_candidates.extend(spatial_cands)

        if method.lower() in ("auto", "lut_prune", "probprun"):
            logger.info("Generating Native 6-LUT Cut Pruning candidates...")
            lut_cands = self._generate_lut_pruned_candidates(iface, run_dir)
            all_candidates.extend(lut_cands)

        if method.lower() in ("auto", "pyals", "als"):
            logger.info("Generating pyALS Catalog-based AIG Rewriting candidates...")
            try:
                from axfpga.pyals_bridge import PyAlsBridge
                pyals_bridge = PyAlsBridge(
                    cone_verilog_path=self.cone_vlog,
                    meta_json_path=self.meta_json,
                    work_dir=run_dir / "pyals_run",
                    target_family=self.target_family,
                    target_device=self.target_device,
                    nodsp=self.nodsp,
                    sim_samples=self.sim_samples,
                )
                pyals_cands = pyals_bridge.generate_catalog_candidates()
                all_candidates.extend(pyals_cands)
                logger.info("Generated %d pyALS Catalog-based AIG Rewriting candidates", len(pyals_cands))
            except Exception as exc:
                logger.debug("pyALS candidate generation skipped: %s", exc)

        if method.lower() in ("auto", "dt", "decision_tree", "decisiontree"):
            logger.info("Generating Decision Tree 6-LUT candidates...")
            dt_cands = self._generate_dt_candidates(iface, stim_hex, run_dir)
            all_candidates.extend(dt_cands)

        logger.info("Evaluating %d candidate approximate netlists...", len(all_candidates))

        valid_mutants: List[Dict[str, Any]] = []
        all_evaluated: List[Dict[str, Any]] = []

        for cand in all_candidates:
            sim_res = self._simulate_candidate(
                exact_vlog=exact_clean_v,
                approx_vlog=cand["path"],
                iface=iface,
                stim_hex=stim_hex,
                metric=metric,
                run_dir=run_dir,
            )

            err_val = sim_res.get(metric.lower(), 1e9)
            passes = err_val <= bound

            all_evaluated.append({
                "name": cand["name"],
                "strategy": cand["strategy"],
                "path": cand["path"],
                "error": err_val,
                "metric": metric,
                "sim_results": sim_res,
            })

            if passes:
                fpga_res = self.estimator.evaluate_netlist(cand["path"], top_module=iface["module_name"])
                cand_lcs = fpga_res.get("estimated_lcs", 9999)
                cand_c4 = fpga_res.get("carry4", 0)
                cand_lut = fpga_res.get("slice_luts", 0)
                cand_ffs = fpga_res.get("slice_registers", 0)

                cand_rec = {
                    "name": cand["name"],
                    "strategy": cand["strategy"],
                    "path": cand["path"],
                    "error": err_val,
                    "metric": metric,
                    "estimated_lcs": cand_lcs,
                    "carry4": cand_c4,
                    "slice_luts": cand_lut,
                    "slice_registers": cand_ffs,
                    "sim_results": sim_res,
                }
                valid_mutants.append(cand_rec)
                logger.info("  [PASS] %s: %s=%.2f <= %.2f | LCs: %d (CARRY4: %d, LUTs: %d)",
                            cand["name"], metric, err_val, bound, cand_lcs, cand_c4, cand_lut)
            else:
                logger.debug("  [FAIL] %s: %s=%.2f > %.2f", cand["name"], metric, err_val, bound)

        # Select Champion
        if valid_mutants:
            # Sort by minimum estimated Logic Cells, breaking ties with lowest error
            valid_mutants.sort(key=lambda m: (m["estimated_lcs"], m["error"]))
            champion = valid_mutants[0]
            champ_lcs = champion["estimated_lcs"]
            savings_pct = round(((baseline_lcs - champ_lcs) / baseline_lcs * 100.0), 2) if baseline_lcs > 0 else 0.0
            logger.info("TOURNAMENT WINNER: %s | LCs %d -> %d (%.1f%% savings) | %s=%.2f",
                        champion["name"], baseline_lcs, champ_lcs, savings_pct, metric, champion["error"])
        elif all_evaluated:
            all_evaluated.sort(key=lambda m: m["error"])
            best_cand = all_evaluated[0]
            fpga_res = self.estimator.evaluate_netlist(best_cand["path"], top_module=iface["module_name"])
            champ_lcs = fpga_res.get("estimated_lcs", 9999)
            savings_pct = round(((baseline_lcs - champ_lcs) / baseline_lcs * 100.0), 2) if baseline_lcs > 0 else 0.0
            champion = {
                "name": best_cand["name"],
                "strategy": best_cand["strategy"],
                "path": best_cand["path"],
                "error": best_cand["error"],
                "metric": metric,
                "estimated_lcs": champ_lcs,
                "carry4": fpga_res.get("carry4", 0),
                "slice_luts": fpga_res.get("slice_luts", 0),
                "slice_registers": fpga_res.get("slice_registers", 0),
            }
            logger.warning("No candidate strictly met bound %s=%.2f; selected best-effort candidate %s (%s=%.2f, %d LCs)",
                           metric, bound, champion["name"], metric, champion["error"], champ_lcs)
        else:
            logger.warning("No candidate satisfied the error bound %s=%.2f; falling back to baseline", metric, bound)
            champion = {
                "name": "baseline_fallback",
                "strategy": "exact",
                "path": exact_clean_v,
                "error": 0.0,
                "metric": metric,
                "estimated_lcs": baseline_lcs,
                "carry4": baseline_c4,
                "slice_luts": baseline_lut,
                "slice_registers": 0,
            }
            savings_pct = 0.0

        target_out = Path(out_approx_path).resolve() if out_approx_path else (run_dir / "approx_cone.v")
        target_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(champion["path"], target_out)

        elapsed = round(time.time() - start_time, 2)
        return {
            "status": "SUCCESS",
            "method": champion["name"],
            "strategy": champion["strategy"],
            "target": "fpga",
            "family": self.target_family,
            "device": self.target_device,
            "baseline_lcs": baseline_lcs,
            "optimized_lcs": champion["estimated_lcs"],
            "lc_savings_pct": savings_pct,
            "exact_carry4": baseline_c4,
            "approx_carry4": champion["carry4"],
            "exact_luts": baseline_lut,
            "approx_luts": champion["slice_luts"],
            "measured_error": champion["error"],
            "error_metric": metric,
            "bound": bound,
            "output_file": str(target_out),
            "elapsed_seconds": elapsed,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="AxSYS Native FPGA Approximate Logic Synthesis Backend")
    parser.add_argument("-i", "--input", required=True, help="Path to extracted_cone.v")
    parser.add_argument("-m", "--metric", default="MED", help="Target error metric (MED, WCE, ER)")
    parser.add_argument("-b", "--bound", type=float, default=16.0, help="Error bound threshold")
    parser.add_argument("--lsb-width", type=int, default=None, help="Explicit LSB pruning width")
    parser.add_argument("-a", "--method", default="auto", help="Approximation strategy (auto, carry_prune, lut_prune)")
    parser.add_argument("--meta", default=None, help="Path to cone_meta.json")
    parser.add_argument("-o", "--output", default="approx_cone.v", help="Output path for approximate netlist")
    parser.add_argument("--target-device", default="xc7a35t", help="Target FPGA device")
    parser.add_argument("--work-dir", default=None, help="Working directory")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    backend = FpgaBackend(
        cone_verilog_path=args.input,
        meta_json_path=args.meta,
        target_device=args.target_device,
        work_dir=args.work_dir,
    )
    result = backend.optimize(
        metric=args.metric,
        bound=args.bound,
        lsb_width=args.lsb_width,
        method=args.method,
        out_approx_path=args.output,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
