"""Realistic Pipeline Trace Extractor.

Simulates the top-level design shell (exact pipeline) to dump active datapath
vectors arriving at the combinational approximation cone into a .hex trace file.
This provides realistic input distributions (e.g., pipeline delay correlation,
spatial locality, valid handshakes) for AxLS Decision Trees and Pruning.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from pyosys import libyosys as ys

logger = logging.getLogger("trace_extractor")


class TraceExtractor:
    """Extracts realistic datapath input traces from the parent pipeline shell."""

    def __init__(
        self,
        top_shell_path: str | Path,
        extracted_cone_path: str | Path,
        meta_json: Optional[str | Path] = None,
        tech_vlog_path: Optional[str | Path] = None,
    ):
        self.top_shell_path = Path(top_shell_path).resolve()
        self.extracted_cone_path = Path(extracted_cone_path).resolve()
        self.meta_json = Path(meta_json).resolve() if meta_json else None
        self.tech_vlog_path = Path(tech_vlog_path).resolve() if tech_vlog_path else None

        self.meta: Dict[str, Any] = {}
        if self.meta_json and self.meta_json.exists():
            with open(self.meta_json, "r", encoding="utf-8") as f:
                self.meta = json.load(f)

    def _inspect_circuit(self) -> Dict[str, Any]:
        """Inspect top shell and extracted cone hierarchy to find ports and instance names."""
        design = ys.Design()
        tech_cmd = f"read_verilog -overwrite {self.tech_vlog_path}; " if (self.tech_vlog_path and self.tech_vlog_path.exists()) else ""
        cmd = (
            f"{tech_cmd}"
            f"read_verilog -sv {self.extracted_cone_path}; "
            f"read_verilog -sv {self.top_shell_path}; "
            f"hierarchy -check -auto-top; "
            f"proc; opt_clean"
        )
        ys.run_pass(cmd, design)

        top_mod = design.top_module()
        top_name = top_mod.name.str().lstrip("\\")

        # Find extracted cone module
        cone_mod = None
        for mod_name, mod in design.modules_.items():
            mname = mod_name.str().lstrip("\\")
            if mname != top_name and ("axc_cone" in mname or "submod" in mname):
                cone_mod = mod
                break

        if cone_mod is None:
            # Fall back to checking any non-top module
            for mod_name, mod in design.modules_.items():
                mname = mod_name.str().lstrip("\\")
                if mname != top_name:
                    cone_mod = mod
                    break

        if cone_mod is None:
            raise RuntimeError("Could not identify extracted cone submodule in design hierarchy")

        cone_name = cone_mod.name.str().lstrip("\\")

        # Find instance name inside top
        cone_inst_name = None
        for cname, cell in top_mod.cells_.items():
            if cell.type.str().lstrip("\\") == cone_name:
                cone_inst_name = cname.str().lstrip("\\")
                break

        if cone_inst_name is None:
            cone_inst_name = cone_name

        # Extract cone input ports in exact declaration order
        cone_inputs: List[Dict[str, Any]] = []
        for port_id in cone_mod.ports:
            wire = cone_mod.wire(port_id)
            if wire.port_input:
                cone_inputs.append({
                    "name": port_id.str().lstrip("\\"),
                    "width": wire.width,
                    "is_signed": bool(wire.is_signed),
                })

        # Extract top inputs and outputs
        top_inputs: List[Dict[str, Any]] = []
        top_outputs: List[Dict[str, Any]] = []
        for wname_id, wire in top_mod.wires_.items():
            name = wname_id.str().lstrip("\\")
            if wire.port_input:
                top_inputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})
            if wire.port_output:
                top_outputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})

        # Detect clk, reset, valid signals
        clk_name = None
        rst_name = None
        rst_active_low = True
        valid_in_name = None

        for inp in top_inputs:
            n = inp["name"].lower()
            if n in ("clk", "clock", "clk_i"):
                clk_name = inp["name"]
            elif n in ("rst_n", "reset_n", "rstn", "rst_b", "reset_b"):
                rst_name = inp["name"]
                rst_active_low = True
            elif n in ("rst", "reset", "rst_i"):
                rst_name = inp["name"]
                rst_active_low = False
            elif "valid" in n or "enable" in n or "en" == n:
                if valid_in_name is None:
                    valid_in_name = inp["name"]

        return {
            "top_name": top_name,
            "cone_name": cone_name,
            "cone_inst_name": cone_inst_name,
            "cone_inputs": cone_inputs,
            "top_inputs": top_inputs,
            "top_outputs": top_outputs,
            "clk_name": clk_name,
            "rst_name": rst_name,
            "rst_active_low": rst_active_low,
            "valid_in_name": valid_in_name,
        }

    def generate_trace_tb(
        self,
        work_dir: Path,
        info: Dict[str, Any],
        out_trace_hex: Path,
        num_samples: int = 500,
        stimulus_file: Optional[Path] = None,
        clk_period_ns: float = 10.0,
    ) -> Path:
        """Generate Verilog testbench that drives top pipeline and logs cone inputs."""
        tb_path = work_dir / "tb_trace_dump.v"
        half_period = max(1.0, clk_period_ns / 2.0)

        top_name = info["top_name"]
        cone_inst_name = info["cone_inst_name"]
        cone_inputs = info["cone_inputs"]
        top_inputs = info["top_inputs"]
        top_outputs = info["top_outputs"]
        clk_name = info["clk_name"]
        rst_name = info["rst_name"]
        rst_active_low = info["rst_active_low"]
        valid_in_name = info["valid_in_name"]

        lines: List[str] = [
            "`timescale 1ns / 1ps",
            "",
            "module tb_trace_dump;",
        ]

        # Declare top module input registers
        special_inputs = set()
        if clk_name:
            special_inputs.add(clk_name)
            lines.append("  reg clk;")
        if rst_name:
            special_inputs.add(rst_name)
            lines.append(f"  reg {rst_name};")

        for inp in top_inputs:
            name = inp["name"]
            width = inp["width"]
            is_signed = inp.get("is_signed", False)
            if name not in special_inputs:
                signed_str = "signed " if is_signed else ""
                range_str = f"[{width-1}:0] " if width > 1 else ""
                lines.append(f"  reg {signed_str}{range_str}{name};")

        # Declare top module output wires
        for out in top_outputs:
            name = out["name"]
            width = out["width"]
            is_signed = out.get("is_signed", False)
            signed_str = "signed " if is_signed else ""
            range_str = f"[{width-1}:0] " if width > 1 else ""
            lines.append(f"  wire {signed_str}{range_str}{name};")

        lines.extend([
            "",
            "  integer trace_file;",
            "  integer sample_count = 0;",
            "  integer cycle_count = 0;",
            "",
            f"  // Instantiation of parent shell: {top_name}",
            f"  {top_name} dut (",
        ])

        conns = []
        for inp in top_inputs:
            n = inp["name"]
            sig = "clk" if n == clk_name else n
            conns.append(f"    .{n}({sig})")
        for out in top_outputs:
            n = out["name"]
            conns.append(f"    .{n}({n})")
        lines.append(",\n".join(conns))
        lines.extend([
            "  );",
            "",
        ])

        # Clock generator
        if clk_name:
            lines.extend([
                f"  always #{half_period:.3f} clk = ~clk;",
                "",
            ])

        # Stimulus and logger
        trace_rel = out_trace_hex.name
        header_names = " ".join([inp["name"] for inp in cone_inputs])

        # Format string for cone input writes
        fmt_specifiers = " ".join(["%h"] * len(cone_inputs))
        fmt_args = ", ".join([f"dut.{cone_inst_name}.{inp['name']}" for inp in cone_inputs])

        data_inputs = [inp for inp in top_inputs if inp["name"] not in special_inputs and inp["name"] != valid_in_name]

        lines.extend([
            "  integer stim_fd = 0;",
            "  integer stim_ret = 0;",
            "",
            "  initial begin",
        ])
        if clk_name:
            lines.append("    clk = 0;")
        if rst_name:
            init_rst = "0" if rst_active_low else "1"
            lines.append(f"    {rst_name} = {init_rst};")

        for inp in top_inputs:
            name = inp["name"]
            if name not in special_inputs:
                lines.append(f"    {name} = 0;")

        lines.extend([
            f"    trace_file = $fopen(\"{trace_rel}\", \"w\");",
            f"    $fwrite(trace_file, \"# {header_names}\\n\");",
        ])

        if stimulus_file and stimulus_file.exists():
            stim_path_str = str(stimulus_file.resolve())
            lines.append(f"    stim_fd = $fopen(\"{stim_path_str}\", \"r\");")

        if rst_name:
            active_rst = "1" if rst_active_low else "0"
            lines.extend([
                "    #20;",
                f"    {rst_name} = {active_rst};",
            ])

        if clk_name is None:
            # Combinational stimulus loop
            lines.extend([
                f"    for (sample_count = 0; sample_count < {num_samples}; sample_count = sample_count + 1) begin",
            ])
            if stimulus_file and stimulus_file.exists() and data_inputs:
                stim_fmt = " ".join(["%h"] * len(data_inputs))
                stim_vars = ", ".join([inp["name"] for inp in data_inputs])
                lines.extend([
                    f"      stim_ret = $fscanf(stim_fd, \"{stim_fmt}\\n\", {stim_vars});",
                    f"      if (stim_ret == -1) begin",
                    f"        $rewind(stim_fd);",
                    f"        stim_ret = $fscanf(stim_fd, \"{stim_fmt}\\n\", {stim_vars});",
                    f"      end",
                ])
            else:
                for inp in top_inputs:
                    lines.append(f"      {inp['name']} = $urandom;")
            lines.extend([
                "      #10;",
                f"      $fwrite(trace_file, \"{fmt_specifiers}\\n\", {fmt_args});",
                "    end",
                "    $fclose(trace_file);",
                "    if (stim_fd) $fclose(stim_fd);",
                "    $finish(0);",
                "  end",
            ])
        else:
            # Sequential pipeline stimulus driven by clock
            lines.extend([
                "  end",
                "",
                "  always @(posedge clk) begin",
            ])
            if rst_name:
                rst_cond = f"{rst_name}" if rst_active_low else f"!{rst_name}"
                lines.append(f"    if ({rst_cond}) begin")
            else:
                lines.append("    begin")

            lines.append("      cycle_count <= cycle_count + 1;")

            # Drive inputs
            if valid_in_name:
                lines.append(f"      {valid_in_name} <= 1'b1;")

            if stimulus_file and stimulus_file.exists() and data_inputs:
                stim_fmt = " ".join(["%h"] * len(data_inputs))
                stim_vars = ", ".join([inp["name"] for inp in data_inputs])
                lines.extend([
                    f"      stim_ret = $fscanf(stim_fd, \"{stim_fmt}\\n\", {stim_vars});",
                    f"      if (stim_ret == -1) begin",
                    f"        $rewind(stim_fd);",
                    f"        stim_ret = $fscanf(stim_fd, \"{stim_fmt}\\n\", {stim_vars});",
                    f"      end",
                ])
            else:
                for inp in data_inputs:
                    name = inp["name"]
                    lines.append(f"      {name} <= $urandom;")

            # Log cone inputs
            valid_check = f"if (dut.{valid_in_name})" if valid_in_name else "if (cycle_count > 2)"
            lines.extend([
                f"      {valid_check} begin",
                f"        $fwrite(trace_file, \"{fmt_specifiers}\\n\", {fmt_args});",
                "        sample_count = sample_count + 1;",
                f"        if (sample_count >= {num_samples}) begin",
                "          $fclose(trace_file);",
                "          if (stim_fd) $fclose(stim_fd);",
                "          $display(\"Successfully dumped %0d trace vectors to %s\", sample_count, \"" + trace_rel + "\");",
                "          $finish(0);",
                "        end",
                "      end",
                "    end",
                "  end",
            ])

        lines.append("endmodule")

        tb_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return tb_path

    def extract_traces(
        self,
        out_trace_hex: str | Path,
        num_samples: int = 500,
        stimulus_file: Optional[str | Path] = None,
        clk_period_ns: float = 10.0,
    ) -> Dict[str, Any]:
        """Run top simulation and capture active pipeline datapath traces for the cone."""
        out_trace_path = Path(out_trace_hex).resolve()
        out_trace_path.parent.mkdir(parents=True, exist_ok=True)

        stim_path = Path(stimulus_file).resolve() if stimulus_file else None

        info = self._inspect_circuit()
        logger.info(
            "Extracting traces: Top=%s, Submod=%s (inst=%s), %d cone inputs",
            info["top_name"], info["cone_name"], info["cone_inst_name"], len(info["cone_inputs"])
        )

        with tempfile.TemporaryDirectory(prefix="trace_dump_") as tmp_dir:
            work_dir = Path(tmp_dir).resolve()
            temp_trace_hex = work_dir / out_trace_path.name

            tb_path = self.generate_trace_tb(
                work_dir=work_dir,
                info=info,
                out_trace_hex=temp_trace_hex,
                num_samples=num_samples,
                stimulus_file=stim_path,
                clk_period_ns=clk_period_ns,
            )

            # Compile with iverilog
            sim_vvp = work_dir / "sim.vvp"
            compile_cmd = [
                "iverilog", "-g2012", "-o", str(sim_vvp),
                str(tb_path),
                str(self.top_shell_path),
                str(self.extracted_cone_path),
            ]
            if self.tech_vlog_path and self.tech_vlog_path.exists():
                compile_cmd.append(str(self.tech_vlog_path))

            logger.info("Compiling trace testbench: %s", " ".join(compile_cmd))
            proc_comp = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=120)
            if proc_comp.returncode != 0:
                logger.error("Trace compilation failed: %s", proc_comp.stderr)
                return {
                    "status": "COMPILATION_ERROR",
                    "stderr": proc_comp.stderr,
                }

            # Run simulation
            logger.info("Running trace extraction simulation in %s", work_dir)
            proc_run = subprocess.run(["vvp", str(sim_vvp)], cwd=str(work_dir), capture_output=True, text=True, timeout=120)
            if proc_run.returncode != 0:
                logger.error("Trace simulation failed: %s", proc_run.stderr)
                return {
                    "status": "SIMULATION_ERROR",
                    "stderr": proc_run.stderr,
                }

            if not temp_trace_hex.exists() or temp_trace_hex.stat().st_size == 0:
                logger.error("Trace output file was not created or is empty: %s", temp_trace_hex)
                return {
                    "status": "EMPTY_TRACE",
                    "stderr": "No trace vectors were written",
                }

            # Copy to final destination
            content = temp_trace_hex.read_text(encoding="utf-8")
            out_trace_path.write_text(content, encoding="utf-8")

            # Count rows (excluding comments)
            data_rows = [line for line in content.splitlines() if line.strip() and not line.startswith("#")]
            logger.info("Trace extraction complete: %d vectors written to %s", len(data_rows), out_trace_path)

            return {
                "status": "SUCCESS",
                "trace_hex_path": str(out_trace_path),
                "num_vectors": len(data_rows),
                "cone_inputs": [inp["name"] for inp in info["cone_inputs"]],
                "top_module": info["top_name"],
                "cone_module": info["cone_name"],
            }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline Trace Extractor CLI")
    parser.add_argument("--top-shell", required=True, help="Path to top_shell.v")
    parser.add_argument("--cone", required=True, help="Path to extracted_cone.v")
    parser.add_argument("-o", "--output", required=True, help="Destination cone_trace.hex path")
    parser.add_argument("-n", "--samples", type=int, default=500, help="Number of trace vectors to capture")
    parser.add_argument("--meta", default=None, help="Path to cone_meta.json")
    parser.add_argument("--tech-v", default=None, help="Technology library Verilog path")
    parser.add_argument("--period", type=float, default=10.0, help="Clock period in ns")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    extractor = TraceExtractor(
        top_shell_path=args.top_shell,
        extracted_cone_path=args.cone,
        meta_json=args.meta,
        tech_vlog_path=args.tech_v,
    )
    result = extractor.extract_traces(
        out_trace_hex=args.output,
        num_samples=args.samples,
        clk_period_ns=args.period,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
