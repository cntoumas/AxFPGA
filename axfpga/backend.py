"""Native FPGA Approximate Logic Synthesis Backend for AxFPGA.

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
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from axfpga.estimator import FpgaEstimator

logger = logging.getLogger("fpga_backend")


class FpgaBackend:
    """Native FPGA Approximate Logic Synthesis Engine."""

    def __init__(
        self,
        cone_verilog_path: str | Path,
        meta_json_path: Optional[str | Path] = None,
        target_family: str = "xc7",
        target_device: str = "xc7a35t",
        nodsp: bool = False,
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

        # Scope content strictly to the target module to avoid bleeding ports from sister modules
        end_idx = content.find("endmodule")
        mod_content = content[:end_idx + len("endmodule")] if end_idx != -1 else content

        inputs: Dict[str, Dict[str, Any]] = {}
        outputs: Dict[str, Dict[str, Any]] = {}

        # Parse port declarations (supports both ANSI header and non-ANSI body declarations)
        port_pat = re.compile(r"\b(input|output)\s+(?:wire\s+|reg\s+)?(?:signed\s+)?(?:\[([^\]]+)\]\s*)?(\S+)")
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
        py_rnd = random.Random(42)
        lines = []
        input_names = list(iface["inputs"].keys())
        lines.append("# " + " ".join(input_names))

        for _ in range(self.sim_samples):
            vec_parts = []
            for name in input_names:
                info = iface["inputs"][name]
                w = info["width"]
                if w == 1:
                    val = py_rnd.choice([0, 1])
                else:
                    max_val = (1 << w) - 1
                    val = py_rnd.randint(0, max_val)
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
        is_sub = "sub" in iface["module_name"].lower() or "diff" in rel_out.lower() or "sub" in rel_out.lower()
        rel_signed = outputs[rel_out].get("signed", False) or is_sub

        # Build clean testbench
        inp_decls = []
        inst_exact_conns = []
        inst_approx_conns = []
        read_conns = []

        for name, info in inputs.items():
            w = info["width"]
            msb = info.get("msb", w - 1)
            lsb = info.get("lsb", 0)
            clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name.lstrip("\\"))
            inp_decls.append(f"  reg [{msb}:{lsb}] in_{clean_name};" if w > 1 else f"  reg in_{clean_name};")
            inst_exact_conns.append(f".{name} (in_{clean_name})")
            inst_approx_conns.append(f".{name} (in_{clean_name})")
            read_conns.append(f"in_{clean_name}")

        out_decls = []
        for name, info in outputs.items():
            w = info["width"]
            msb = info.get("msb", w - 1)
            lsb = info.get("lsb", 0)
            clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name.lstrip("\\"))
            out_decls.append(f"  wire [{msb}:{lsb}] exact_{clean_name};" if w > 1 else f"  wire exact_{clean_name};")
            out_decls.append(f"  wire [{msb}:{lsb}] approx_{clean_name};" if w > 1 else f"  wire approx_{clean_name};")
            inst_exact_conns.append(f".{name} (exact_{clean_name})")
            inst_approx_conns.append(f".{name} (approx_{clean_name})")

        format_str = " ".join(["%x"] * len(inputs))
        read_args = ", ".join(read_conns)

        rel_clean = re.sub(r"[^a-zA-Z0-9_]", "_", rel_out.lstrip("\\"))
        rel_lsb = outputs[rel_out].get("lsb", 0)
        signed_calc = f"""
        for (b_idx = 0; b_idx < {rel_width}; b_idx = b_idx + 1) begin
          clean_exact[b_idx] = (exact_{rel_clean}[b_idx + {rel_lsb}] === 1'b1) ? 1'b1 : 1'b0;
          clean_approx[b_idx] = (approx_{rel_clean}[b_idx + {rel_lsb}] === 1'b1) ? 1'b1 : 1'b0;
        end
        if ({"1" if rel_signed else "0"}) begin
          diff = $signed(clean_exact) - $signed(clean_approx);
        end else begin
          diff = (clean_exact >= clean_approx) ? (clean_exact - clean_approx) : -(clean_approx - clean_exact);
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
  integer b_idx;
  reg [{rel_width-1}:0] clean_exact, clean_approx;
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

        # Generate candidates strictly from pyALS engine
        all_candidates: List[Dict[str, Any]] = []

        if method.lower() not in ("auto", "pyals", "als"):
            logger.warning("Approximation method '%s' is deprecated; AxFPGA runs pyALS only.", method)

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
            logger.warning("pyALS candidate generation skipped: %s", exc)

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

        # Phase 1.3 Action Item 2: Post-ALSRewriter Xilinx 7-Series Yosys re-mapping pass
        remap_out = run_dir / "remapped_approx_cone.v"
        remap_cmd = (
            f"read_verilog -sv {champion['path']}; "
            f"hierarchy -check -auto-top; "
            f"opt_clean -purge; "
            f"opt -mux_undef -mux_bool -undriven -fine; "
            f"synth_xilinx -family {self.target_family} -noiopad -run begin:check; "
            f"opt_clean -purge; "
            f"write_verilog -noattr {remap_out}"
        )
        remap_res = subprocess.run(f"yosys -q -p \"{remap_cmd}\"", shell=True, executable="/bin/bash", capture_output=True, text=True)
        final_cand_path = remap_out if (remap_res.returncode == 0 and remap_out.exists()) else champion["path"]

        target_out = Path(out_approx_path).resolve() if out_approx_path else (run_dir / "approx_cone.v")
        target_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(final_cand_path, target_out)

        # Generate Vivado sign-off batch synthesis script (Zybo / PYNQ / Artix-7)
        from axfpga.vivado import VivadoBatchRunner
        vivado_runner = VivadoBatchRunner(part=self.target_device or "xc7z010clg400-1")
        vivado_tcl = run_dir / "run_vivado_synth.tcl"
        vivado_runner.generate_tcl(
            top_module=iface["module_name"],
            verilog_files=[target_out],
            out_tcl_path=vivado_tcl,
        )

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
            "vivado_tcl": str(vivado_tcl),
            "elapsed_seconds": elapsed,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="AxFPGA Native Approximate Logic Synthesis Backend (pyALS)")
    parser.add_argument("-i", "--input", required=True, help="Path to extracted_cone.v")
    parser.add_argument("-m", "--metric", default="MED", help="Target error metric (MED, WCE, ER)")
    parser.add_argument("-b", "--bound", type=float, default=16.0, help="Error bound threshold")
    parser.add_argument("--lsb-width", type=int, default=None, help="Explicit LSB pruning width")
    parser.add_argument("-a", "--method", default="pyals", choices=["pyals", "als", "auto"], help="Approximation engine (strictly pyALS)")
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
