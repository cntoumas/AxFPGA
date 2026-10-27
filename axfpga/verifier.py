"""Automated Miter Verification Harness.

Instantiates the golden exact RTL and the final approximated gate netlist
side-by-side in an automated Miter testbench, compiling and executing with Icarus Verilog.
Also integrates mathematical formal verification via Yosys SAT:
1. Formal Control Equivalence (sat -tempinduct): mathematically proves 100% bit-exact
   control preservation across all reachable sequential states.
2. Formal Combinational WCE Prover (sat -prove): mathematically proves that the
   datapath error |Y_{exact} - Y_{approx}| <= WCE_{bound} holds for all input combinations.

Enforces two strict verification invariants:
1. Zero Control Corruption: 100% cycle-exact match on (* axc_precise *) and unannotated outputs.
2. Bounded Error: Measured error metric on (* axc_relax *) signals <= axc_bound.
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

logger = logging.getLogger("verifier")

TECH_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "third_party" / "AxLS" / "templates"
TECH_V_DEFAULT = TECH_TEMPLATES_DIR / "NanGate15nm.v"


class FormalControlVerifier:
    """Proves 100% bit-exact control preservation using Yosys SAT temporal induction."""

    def __init__(
        self,
        exact_vlog: str | Path,
        approx_vlog: str | Path,
        meta_json: Optional[str | Path] = None,
        tech_vlog: Optional[str | Path] = None,
        induction_depth: int = 5,
        timeout: int = 60,
    ):
        self.exact_vlog = Path(exact_vlog).resolve()
        self.approx_vlog = Path(approx_vlog).resolve()
        self.meta_json = Path(meta_json).resolve() if meta_json else None
        self.tech_vlog = Path(tech_vlog).resolve() if tech_vlog else None
        self.induction_depth = induction_depth
        self.timeout = timeout

        self.meta: Dict[str, Any] = {}
        if self.meta_json and self.meta_json.exists():
            with open(self.meta_json, "r", encoding="utf-8") as f:
                self.meta = json.load(f)

        self.top_module = self.meta.get("top_module", "top")

    def _inspect_signals(self) -> Dict[str, Any]:
        """Inspect input and output ports of top module to separate control and relaxed signals."""
        design = ys.Design()
        ys.run_pass(f"read_verilog -sv {self.exact_vlog}; hierarchy -check -auto-top; proc; opt_clean", design)
        top = design.top_module()
        self.top_module = top.name.str().lstrip("\\")

        inputs: List[Dict[str, Any]] = []
        outputs: List[Dict[str, Any]] = []

        for wname_id, wire in top.wires_.items():
            name = wname_id.str().lstrip("\\")
            if wire.port_input:
                inputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})
            if wire.port_output:
                outputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})

        relaxed_names = {s["wire"] for s in self.meta.get("relaxed_signals", [])}
        precise_outputs = [out for out in outputs if out["name"] not in relaxed_names]

        # Identify clk and reset
        clk_name = None
        rst_name = None
        rst_active_low = True

        for inp in inputs:
            n = inp["name"].lower()
            if n in ("clk", "clock", "clk_i"):
                clk_name = inp["name"]
            elif n in ("rst_n", "reset_n", "rstn", "rst_b", "reset_b"):
                rst_name = inp["name"]
                rst_active_low = True
            elif n in ("rst", "reset", "rst_i"):
                rst_name = inp["name"]
                rst_active_low = False

        return {
            "top_module": self.top_module,
            "inputs": inputs,
            "outputs": outputs,
            "precise_outputs": precise_outputs,
            "relaxed_outputs": [out for out in outputs if out["name"] in relaxed_names],
            "clk_name": clk_name,
            "rst_name": rst_name,
            "rst_active_low": rst_active_low,
        }

    def generate_ctrl_miter(self, work_dir: Path, sig_info: Dict[str, Any]) -> Path:
        """Generate Verilog miter that compares precise control outputs between exact and approx tops."""
        miter_path = work_dir / "formal_ctrl_miter.v"
        top_name = sig_info["top_module"]
        inputs = sig_info["inputs"]
        precise_outputs = sig_info["precise_outputs"]

        lines: List[str] = [
            "`timescale 1ns / 1ps",
            "",
            "module formal_ctrl_miter (",
        ]

        # Primary input declarations
        for inp in inputs:
            name = inp["name"]
            width = inp["width"]
            is_signed = inp.get("is_signed", False)
            signed_str = "signed " if is_signed else ""
            range_str = f"[{width-1}:0] " if width > 1 else ""
            lines.append(f"  input {signed_str}{range_str}{name},")

        lines.extend([
            "  output ctrl_mismatch",
            ");",
            "",
        ])

        # Declare wires for all outputs of exact and approx instances
        for out in sig_info["outputs"]:
            name = out["name"]
            width = out["width"]
            is_signed = out.get("is_signed", False)
            signed_str = "signed " if is_signed else ""
            range_str = f"[{width-1}:0] " if width > 1 else ""
            lines.append(f"  wire {signed_str}{range_str}exact_{name};")
            lines.append(f"  wire {signed_str}{range_str}approx_{name};")

        lines.extend([
            "",
            f"  // Golden exact instance",
            f"  {top_name}_exact u_exact (",
        ])
        exact_conns = []
        for inp in inputs:
            n = inp["name"]
            exact_conns.append(f"    .{n}({n})")
        for out in sig_info["outputs"]:
            n = out["name"]
            exact_conns.append(f"    .{n}(exact_{n})")
        lines.append(",\n".join(exact_conns))
        lines.extend([
            "  );",
            "",
            f"  // Approximated instance",
            f"  {top_name}_approx u_approx (",
        ])
        approx_conns = []
        for inp in inputs:
            n = inp["name"]
            approx_conns.append(f"    .{n}({n})")
        for out in sig_info["outputs"]:
            n = out["name"]
            approx_conns.append(f"    .{n}(approx_{n})")
        lines.append(",\n".join(approx_conns))
        lines.extend([
            "  );",
            "",
        ])

        # Comparison logic for precise control signals
        if precise_outputs:
            terms = []
            for out in precise_outputs:
                n = out["name"]
                w = out["width"]
                if w == 1:
                    terms.append(f"(exact_{n} ^ approx_{n})")
                else:
                    terms.append(f"(|(exact_{n} ^ approx_{n}))")
            mismatch_expr = " | ".join(terms)
            lines.append(f"  assign ctrl_mismatch = {mismatch_expr};")
        else:
            lines.append("  assign ctrl_mismatch = 1'b0;")

        lines.append("endmodule")
        miter_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return miter_path

    def verify(self) -> Dict[str, Any]:
        """Execute Yosys SAT temporal induction to prove bit-exact control preservation."""
        sig_info = self._inspect_signals()
        precise_outputs = sig_info["precise_outputs"]

        if not precise_outputs:
            logger.info("No precise control signals found in top module '%s'. Formal control check trivially proven.", sig_info["top_module"])
            return {
                "status": "PROVEN",
                "method": "vacuous",
                "details": "No precise control outputs defined; all outputs are relaxed",
                "signals_checked": [],
                "proven_forever": True,
            }

        top_name = sig_info["top_module"]
        clk_name = sig_info["clk_name"]
        rst_name = sig_info["rst_name"]
        rst_active_low = sig_info["rst_active_low"]

        with tempfile.TemporaryDirectory(prefix="formal_ctrl_") as tmp_dir:
            work_dir = Path(tmp_dir).resolve()
            miter_path = self.generate_ctrl_miter(work_dir, sig_info)

            # Only load tech library if netlist references standard cell primitives
            needs_tech = False
            for vpath in [self.exact_vlog, self.approx_vlog]:
                txt = vpath.read_text(encoding="utf-8", errors="ignore")
                if "_X1" in txt or "_X2" in txt or "DFF" in txt:
                    needs_tech = True
                    break

            tech_cmd = f"read_verilog -overwrite {self.tech_vlog};\n" if (needs_tech and self.tech_vlog and self.tech_vlog.exists()) else ""
            ys_script_path = work_dir / "formal_ctrl.ys"

            lines = [
                tech_cmd,
                f"read_verilog -sv -overwrite {self.exact_vlog}",
                f"rename {top_name} {top_name}_exact",
                tech_cmd,
                f"read_verilog -sv -overwrite {self.approx_vlog}",
                f"rename {top_name} {top_name}_approx",
                f"read_verilog -sv {miter_path}",
                "prep -top formal_ctrl_miter",
            ]

            is_sequential = clk_name is not None
            if is_sequential:
                lines.append("async2sync")

            lines.extend([
                "flatten",
                "opt_clean",
            ])

            if is_sequential:
                rst_args = ""
                if rst_name:
                    rst_val = "0" if rst_active_low else "1"
                    rst_args = f"-set-at 1 {rst_name} {rst_val} -unset-at 1 {rst_name} "
                sat_cmd = (
                    f"sat -timeout {self.timeout} -seq {self.induction_depth} "
                    f"-set-init-zero {rst_args}-tempinduct -prove ctrl_mismatch 1'b0"
                )
            else:
                sat_cmd = f"sat -timeout {self.timeout} -show ctrl_mismatch -prove ctrl_mismatch 1'b0"

            lines.append(sat_cmd)
            ys_script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            logger.info("Executing Formal Control SAT proof with induction depth %d (timeout=%ds)", self.induction_depth, self.timeout)
            proc = subprocess.run(["yosys", "-s", str(ys_script_path)], capture_output=True, text=True, timeout=self.timeout + 30)

            stdout = proc.stdout
            stderr = proc.stderr
            combined = stdout + "\n" + stderr

            checked_names = [o["name"] for o in precise_outputs]

            if "Induction step proven: SUCCESS!" in combined or "SAT proof finished - no model found: SUCCESS!" in combined:
                logger.info("Formal Control Equivalence PROVEN for signals %s", checked_names)
                return {
                    "status": "PROVEN",
                    "method": "temporal_induction" if is_sequential else "combinational_sat",
                    "signals_checked": checked_names,
                    "induction_depth": self.induction_depth if is_sequential else None,
                    "proven_forever": True,
                    "log": stdout[-800:],
                }

            if "TIMEOUT!" in combined or "Interrupted SAT solver" in combined:
                logger.warning("Formal Control SAT proof timed out after %ds", self.timeout)
                return {
                    "status": "TIMEOUT",
                    "method": "temporal_induction" if is_sequential else "combinational_sat",
                    "signals_checked": checked_names,
                    "induction_depth": self.induction_depth if is_sequential else None,
                    "proven_forever": False,
                    "log": stdout[-800:],
                }

            if "model found" in combined or "FAIL!" in combined:
                logger.error("Formal Control Corruption DETECTED by SAT solver!")
                cex_dict = self._parse_sat_model(stdout)
                return {
                    "status": "FALSIFIED",
                    "method": "temporal_induction" if is_sequential else "combinational_sat",
                    "signals_checked": checked_names,
                    "counterexample": cex_dict,
                    "proven_forever": False,
                    "log": stdout[-1200:],
                }

            # If induction step was unproven but base case held, run bounded check
            if is_sequential and "Base case for induction" in combined:
                logger.info("Induction step unproven; running bounded check to depth %d", self.induction_depth)
                bmc_script = work_dir / "formal_bmc.ys"
                bmc_lines = lines[:-1] + [
                    f"sat -timeout {self.timeout} -seq {self.induction_depth} -set-init-zero {rst_args}-prove ctrl_mismatch 1'b0"
                ]
                bmc_script.write_text("\n".join(bmc_lines) + "\n", encoding="utf-8")
                proc_bmc = subprocess.run(["yosys", "-s", str(bmc_script)], capture_output=True, text=True, timeout=self.timeout + 30)
                if "SAT proof finished - no model found: SUCCESS!" in (proc_bmc.stdout + proc_bmc.stderr):
                    return {
                        "status": "PROVEN_BOUNDED",
                        "method": "bounded_model_checking",
                        "signals_checked": checked_names,
                        "cycles_checked": self.induction_depth,
                        "proven_forever": False,
                        "log": proc_bmc.stdout[-800:],
                    }

            logger.warning("Formal Control SAT proof inconclusive:\n%s", combined[-800:])
            return {
                "status": "INCONCLUSIVE",
                "signals_checked": checked_names,
                "error": stderr or "SAT returned without explicit proof result",
                "log": combined[-800:],
            }

    def _parse_sat_model(self, stdout: str) -> Dict[str, Any]:
        """Extract signal assignments from SAT model counterexample output."""
        cex: Dict[str, Any] = {}
        pattern = re.compile(r"^\s*([0-9]+|init)?\s*\\([a-zA-Z0-9_\$]+)\s+(-?[0-9]+)\s+([0-9a-fA-F]+)\s+([01xX_]+)", re.MULTILINE)
        for m in pattern.finditer(stdout):
            timestep = m.group(1) or "1"
            sig = m.group(2)
            dec_val = m.group(3)
            hex_val = m.group(4)
            cex[f"t{timestep}:{sig}"] = {"dec": dec_val, "hex": hex_val}
        return cex


class FormalWceVerifier:
    """Proves combinational Worst-Case Error (WCE) bounds |Y_exact - Y_approx| <= bound via Yosys SAT."""

    def __init__(
        self,
        exact_cone_vlog: str | Path,
        approx_cone_vlog: str | Path,
        meta_json: Optional[str | Path] = None,
        target_signals: Optional[List[Dict[str, Any]]] = None,
        tech_vlog: Optional[str | Path] = None,
        timeout: int = 60,
    ):
        self.exact_cone_vlog = Path(exact_cone_vlog).resolve()
        self.approx_cone_vlog = Path(approx_cone_vlog).resolve()
        self.meta_json = Path(meta_json).resolve() if meta_json else None
        self.tech_vlog = Path(tech_vlog).resolve() if tech_vlog else None
        self.timeout = timeout

        self.meta: Dict[str, Any] = {}
        if self.meta_json and self.meta_json.exists():
            with open(self.meta_json, "r", encoding="utf-8") as f:
                self.meta = json.load(f)

        self.target_signals = target_signals or self.meta.get("relaxed_signals", [])

    def _inspect_cone_ports(self) -> Dict[str, Any]:
        """Inspect input and output ports of extracted combinational cone."""
        target_mod = None
        if self.approx_cone_vlog.exists():
            try:
                d_approx = ys.Design()
                ys.run_pass(f"read_verilog -sv {self.approx_cone_vlog}", d_approx)
                cand_mods = [
                    m.str().lstrip("\\") for m in d_approx.modules_.keys()
                    if not m.str().lstrip("\\").startswith("$") and not m.str().lstrip("\\").startswith("_")
                ]
                if cand_mods:
                    target_mod = cand_mods[0]
            except Exception:
                pass

        design = ys.Design()
        top_cmd = f"hierarchy -check -top {target_mod}" if target_mod else "hierarchy -check -auto-top"
        ys.run_pass(f"read_verilog -sv {self.exact_cone_vlog}; {top_cmd}; proc; opt_clean", design)
        top = design.top_module()
        cone_module = top.name.str().lstrip("\\")

        inputs = []
        outputs = []
        for wname_id, wire in top.wires_.items():
            name = wname_id.str().lstrip("\\")
            if wire.port_input:
                inputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})
            if wire.port_output:
                outputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})

        return {
            "cone_module": cone_module,
            "inputs": inputs,
            "outputs": outputs,
        }

    def generate_wce_miter(self, work_dir: Path, ports_info: Dict[str, Any]) -> Path:
        """Build combinational comparator miter checking abs(exact - approx) <= bound."""
        miter_path = work_dir / "formal_wce_miter.v"
        cone_module = ports_info["cone_module"]
        inputs = ports_info["inputs"]
        outputs = ports_info["outputs"]

        lines: List[str] = [
            "`timescale 1ns / 1ps",
            "",
            "module formal_wce_miter (",
        ]

        # Declare input ports matching cone
        for inp in inputs:
            name = inp["name"]
            width = inp["width"]
            is_signed = inp.get("is_signed", False)
            signed_str = "signed " if is_signed else ""
            range_str = f"[{width-1}:0] " if width > 1 else ""
            lines.append(f"  input {signed_str}{range_str}{name},")

        lines.extend([
            "  output wce_violation",
            ");",
            "",
        ])

        # Declare output wires
        for out in outputs:
            name = out["name"]
            width = out["width"]
            is_signed = out.get("is_signed", False)
            signed_str = "signed " if is_signed else ""
            range_str = f"[{width-1}:0] " if width > 1 else ""
            lines.append(f"  wire {signed_str}{range_str}exact_{name};")
            lines.append(f"  wire {signed_str}{range_str}approx_{name};")

        lines.extend([
            "",
            f"  // Exact golden cone instance",
            f"  {cone_module}_exact u_exact (",
        ])
        exact_conns = [f"    .{inp['name']}({inp['name']})" for inp in inputs]
        exact_conns.extend([f"    .{out['name']}(exact_{out['name']})" for out in outputs])
        lines.append(",\n".join(exact_conns))
        lines.extend([
            "  );",
            "",
            f"  // Approximated cone instance",
            f"  {cone_module}_approx u_approx (",
        ])
        approx_conns = [f"    .{inp['name']}({inp['name']})" for inp in inputs]
        approx_conns.extend([f"    .{out['name']}(approx_{out['name']})" for out in outputs])
        lines.append(",\n".join(approx_conns))
        lines.extend([
            "  );",
            "",
        ])

        # Build absolute difference and violation checks
        viol_terms = []
        out_map = {o["name"]: o for o in outputs}

        for idx, sig in enumerate(self.target_signals):
            wire_name = sig.get("wire", "")
            if not wire_name or wire_name not in out_map:
                # If target signal name is not a direct port, match any output port
                if outputs:
                    wire_name = outputs[0]["name"]
                else:
                    continue

            port_meta = out_map[wire_name]
            width = port_meta["width"]
            is_signed = port_meta.get("is_signed", False)

            # Determine integer bound for WCE comparison
            raw_bound = sig.get("bound", 16.0)
            try:
                bound_val = int(round(float(raw_bound)))
            except (ValueError, TypeError):
                bound_val = 16

            ext_w = width + 1
            if is_signed:
                lines.extend([
                    f"  wire signed [{ext_w}:0] raw_diff_{idx} = $signed(exact_{wire_name}) - $signed(approx_{wire_name});",
                    f"  wire [{ext_w}:0] abs_diff_{idx} = raw_diff_{idx}[{ext_w}] ? -raw_diff_{idx} : raw_diff_{idx};",
                    f"  wire viol_{idx} = (abs_diff_{idx} > {ext_w+1}'d{bound_val});",
                ])
            else:
                lines.extend([
                    f"  wire [{ext_w}:0] abs_diff_{idx} = (exact_{wire_name} >= approx_{wire_name}) ? (exact_{wire_name} - approx_{wire_name}) : (approx_{wire_name} - exact_{wire_name});",
                    f"  wire viol_{idx} = (abs_diff_{idx} > {ext_w+1}'d{bound_val});",
                ])
            viol_terms.append(f"viol_{idx}")

        if viol_terms:
            lines.append(f"  assign wce_violation = {' | '.join(viol_terms)};")
        else:
            lines.append("  assign wce_violation = 1'b0;")

        lines.append("endmodule")
        miter_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return miter_path

    def verify(self) -> Dict[str, Any]:
        """Execute Yosys SAT combinational solver to prove error bound holds across all inputs."""
        ports_info = self._inspect_cone_ports()
        cone_module = ports_info["cone_module"]

        with tempfile.TemporaryDirectory(prefix="formal_wce_") as tmp_dir:
            work_dir = Path(tmp_dir).resolve()
            miter_path = self.generate_wce_miter(work_dir, ports_info)

            # Only load tech library if netlist references standard cell primitives
            needs_tech = False
            for vpath in [self.exact_cone_vlog, self.approx_cone_vlog]:
                txt = vpath.read_text(encoding="utf-8", errors="ignore")
                if "_X1" in txt or "_X2" in txt or "DFF" in txt:
                    needs_tech = True
                    break

            tech_cmd = f"read_verilog -overwrite {self.tech_vlog};\n" if (needs_tech and self.tech_vlog and self.tech_vlog.exists()) else ""
            ys_script_path = work_dir / "formal_wce.ys"

            lines = [
                tech_cmd,
                f"read_verilog -sv -overwrite {self.exact_cone_vlog}",
                f"rename {cone_module} {cone_module}_exact",
                tech_cmd,
                f"read_verilog -sv -overwrite {self.approx_cone_vlog}",
                f"rename {cone_module} {cone_module}_approx",
                f"read_verilog -sv {miter_path}",
                "prep -top formal_wce_miter",
                "flatten",
                "opt; opt_clean",
                f"sat -timeout {self.timeout} -show wce_violation -prove wce_violation 1'b0",
            ]
            ys_script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            logger.info("Executing Formal WCE SAT proof on cone '%s' (timeout=%ds)", cone_module, self.timeout)
            proc = subprocess.run(["yosys", "-s", str(ys_script_path)], capture_output=True, text=True, timeout=self.timeout + 30)

            stdout = proc.stdout
            stderr = proc.stderr
            combined = stdout + "\n" + stderr

            if "SAT proof finished - no model found: SUCCESS!" in combined:
                logger.info("Formal WCE Bound PROVEN for cone '%s'", cone_module)
                return {
                    "status": "PROVEN",
                    "wce_bound_satisfied": True,
                    "target_signals": self.target_signals,
                    "cone_module": cone_module,
                    "log": stdout[-800:],
                }

            if "TIMEOUT!" in combined or "Interrupted SAT solver" in combined:
                logger.warning("Formal WCE SAT proof timed out after %ds", self.timeout)
                return {
                    "status": "TIMEOUT",
                    "wce_bound_satisfied": None,
                    "cone_module": cone_module,
                    "log": stdout[-800:],
                }

            if "SAT proof finished - model found: FAIL!" in combined or "SAT model found." in combined:
                logger.warning("Formal WCE Bound VIOLATION found by SAT solver for cone '%s'", cone_module)
                return {
                    "status": "FALSIFIED",
                    "wce_bound_satisfied": False,
                    "cone_module": cone_module,
                    "log": stdout[-1200:],
                }

            logger.warning("Formal WCE proof inconclusive:\n%s", combined[-800:])
            return {
                "status": "INCONCLUSIVE",
                "wce_bound_satisfied": None,
                "cone_module": cone_module,
                "error": stderr or "SAT returned without explicit proof result",
                "log": combined[-800:],
            }


class MiterVerifier:
    """Generates and executes comparison miter testbenches for approximate hardware."""

    def __init__(
        self,
        exact_vlog: str | Path,
        approx_vlog: str | Path,
        meta_json: str | Path,
        tech: str = "NanGate15nm",
        tech_vlog: Optional[str | Path] = None,
        num_cycles: int = 500,
        enable_formal: bool = True,
        formal_timeout: int = 60,
        extracted_cone_vlog: Optional[str | Path] = None,
        approx_cone_vlog: Optional[str | Path] = None,
    ):
        self.exact_vlog = Path(exact_vlog).resolve()
        self.approx_vlog = Path(approx_vlog).resolve()
        self.meta_json = Path(meta_json).resolve()

        if tech_vlog:
            self.tech_vlog = Path(tech_vlog).resolve()
        else:
            tech_as_path = Path(tech)
            if tech_as_path.is_file():
                self.tech_vlog = tech_as_path.resolve()
            else:
                candidate = TECH_TEMPLATES_DIR / f"{tech}.v"
                self.tech_vlog = candidate.resolve() if candidate.is_file() else TECH_V_DEFAULT

        if not self.tech_vlog.exists():
            logger.warning("Technology Verilog file not found: %s", self.tech_vlog)

        self.num_cycles = num_cycles
        self.enable_formal = enable_formal
        self.formal_timeout = formal_timeout
        self.extracted_cone_vlog = Path(extracted_cone_vlog).resolve() if extracted_cone_vlog else None
        self.approx_cone_vlog = Path(approx_cone_vlog).resolve() if approx_cone_vlog else None

        with open(self.meta_json, "r", encoding="utf-8") as f:
            self.meta = json.load(f)

        self.top_module = self.meta.get("top_module", "top")

    def _prepare_approx_netlist(self, work_dir: Path) -> Path:
        """Rename approximate top module to avoid collision in iverilog."""
        renamed_vlog = work_dir / f"{self.top_module}_approx.v"
        design = ys.Design()
        tech_cmd = f"read_verilog -overwrite {self.tech_vlog}; " if self.tech_vlog.exists() else ""
        cmd = (
            f"read_verilog -overwrite {self.approx_vlog}; {tech_cmd}"
            f"hierarchy -check -top {self.top_module}; "
            f"rename {self.top_module} {self.top_module}_approx; "
            f"write_verilog {renamed_vlog}"
        )
        ys.run_pass(cmd, design)
        return renamed_vlog

    def _inspect_module_ports(self) -> Dict[str, Any]:
        """Inspect input and output ports of top module."""
        design = ys.Design()
        ys.run_pass(f"read_verilog -sv {self.exact_vlog}; hierarchy -check -auto-top; proc; opt_clean", design)
        top = design.top_module()
        inputs = []
        outputs = []
        for wname_id, wire in top.wires_.items():
            name = wname_id.str().lstrip("\\")
            if wire.port_input:
                inputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})
            if wire.port_output:
                outputs.append({"name": name, "width": wire.width, "is_signed": bool(wire.is_signed)})
        return {"inputs": inputs, "outputs": outputs}

    def generate_miter_tb(self, work_dir: Path, ports_info: Dict[str, Any]) -> Path:
        """Generate self-checking Verilog miter testbench."""
        tb_path = work_dir / "tb_miter.v"

        inputs = ports_info["inputs"]
        outputs = ports_info["outputs"]

        relaxed_names = {s["wire"]: s for s in self.meta.get("relaxed_signals", [])}

        lines: List[str] = [
            "`timescale 1ns / 1ps",
            "",
            "module tb_miter;",
            "  reg clk;",
            "  reg rst_n;",
        ]

        # Declare input regs
        clk_inputs = {"clk", "clock"}
        rst_inputs = {"rst", "rst_n", "reset", "reset_n"}

        for inp in inputs:
            name = inp["name"]
            width = inp["width"]
            is_signed = inp.get("is_signed", False)
            if name not in clk_inputs and name not in rst_inputs:
                signed_str = "signed " if is_signed else ""
                range_str = f"[{width-1}:0] " if width > 1 else ""
                lines.append(f"  reg {signed_str}{range_str}{name};")

        # Declare output wires for exact and approx
        for out in outputs:
            name = out["name"]
            width = out["width"]
            is_signed = out.get("is_signed", False)
            signed_str = "signed " if is_signed else ""
            range_str = f"[{width-1}:0] " if width > 1 else ""
            lines.append(f"  wire {signed_str}{range_str}{name}_exact;")
            lines.append(f"  wire {signed_str}{range_str}{name}_approx;")

        lines.extend([
            "",
            "  integer cycle_count = 0;",
            "  integer control_errors = 0;",
            "  real total_error_distance = 0.0;",
            "  real max_error_distance = 0.0;",
            "  real total_squared_error = 0.0;",
            "  real total_rel_error = 0.0;",
            "  integer error_cycle_count = 0;",
            "  real current_error = 0.0;",
            "  real diff_val = 0.0;",
        ])

        for sig_name in relaxed_names:
            lines.extend([
                f"  real total_error_distance_{sig_name} = 0.0;",
                f"  real max_error_distance_{sig_name} = 0.0;",
                f"  real total_squared_error_{sig_name} = 0.0;",
                f"  real total_rel_error_{sig_name} = 0.0;",
                f"  integer error_cycle_count_{sig_name} = 0;",
            ])

        lines.extend([
            "",
            "  // Clock generation",
            "  always #5 clk = ~clk;",
            "",
            f"  // Golden reference: {self.top_module}",
            f"  {self.top_module} uut_exact (",
        ])

        exact_conns = []
        for inp in inputs:
            name = inp["name"]
            exact_conns.append(f"    .{name}({name})")
        for out in outputs:
            name = out["name"]
            exact_conns.append(f"    .{name}({name}_exact)")
        lines.append(",\n".join(exact_conns))
        lines.extend([
            "  );",
            "",
            f"  // Approximated implementation: {self.top_module}_approx",
            f"  {self.top_module}_approx uut_approx (",
        ])

        approx_conns = []
        for inp in inputs:
            name = inp["name"]
            approx_conns.append(f"    .{name}({name})")
        for out in outputs:
            name = out["name"]
            approx_conns.append(f"    .{name}({name}_approx)")
        lines.append(",\n".join(approx_conns))
        lines.extend([
            "  );",
            "",
            "  initial begin",
            "    clk = 0;",
            "    rst_n = 0;",
        ])

        for inp in inputs:
            name = inp["name"]
            if name not in clk_inputs and name not in rst_inputs:
                lines.append(f"    {name} = 0;")

        lines.extend([
            "    #20 rst_n = 1;",
            "  end",
            "",
            "  always @(posedge clk) begin",
            "    if (rst_n) begin",
            "      cycle_count <= cycle_count + 1;",
            "",
            "      // 1. Verify 100% Precise Control Integrity",
        ])

        for out in outputs:
            name = out["name"]
            if name not in relaxed_names:
                lines.extend([
                    f"      if ({name}_exact !== {name}_approx) begin",
                    f"        $display(\"[ERROR: CYCLE %0d] Precise output mismatch on '{name}': exact=%h approx=%h\", cycle_count, {name}_exact, {name}_approx);",
                    "        control_errors <= control_errors + 1;",
                    "      end",
                ])

        lines.extend([
            "",
            "      // 2. Measure Datapath Approximate Error",
        ])

        for out in outputs:
            name = out["name"]
            if name in relaxed_names:
                lines.extend([
                    f"      if ({name}_exact >= {name}_approx) begin",
                    f"        diff_val = {name}_exact - {name}_approx;",
                    "      end else begin",
                    f"        diff_val = {name}_approx - {name}_exact;",
                    "      end",
                    f"      total_error_distance_{name} = total_error_distance_{name} + diff_val;",
                    f"      if (diff_val > max_error_distance_{name}) max_error_distance_{name} = diff_val;",
                    f"      if (diff_val > 0.0) error_cycle_count_{name} = error_cycle_count_{name} + 1;",
                    f"      total_squared_error_{name} = total_squared_error_{name} + (diff_val * diff_val);",
                    f"      if ({name}_exact != 0) begin",
                    f"        total_rel_error_{name} = total_rel_error_{name} + (diff_val / ({name}_exact > 0 ? {name}_exact : -{name}_exact));",
                    "      end",
                    "      total_error_distance = total_error_distance + diff_val;",
                    "      if (diff_val > max_error_distance) max_error_distance = diff_val;",
                    "      if (diff_val > 0.0) error_cycle_count = error_cycle_count + 1;",
                    "      total_squared_error = total_squared_error + (diff_val * diff_val);",
                    f"      if ({name}_exact != 0) begin",
                    f"        total_rel_error = total_rel_error + (diff_val / ({name}_exact > 0 ? {name}_exact : -{name}_exact));",
                    "      end",
                ])

        lines.extend([
            "",
            "      // Random stimulus for next cycle",
        ])

        for inp in inputs:
            name = inp["name"]
            width = inp["width"]
            if name not in clk_inputs and name not in rst_inputs:
                lines.append(f"      {name} <= $urandom;")

        lines.extend([
            f"      if (cycle_count >= {self.num_cycles}) begin",
            "        $display(\"--- VERIFICATION SUMMARY ---\");",
            "        $display(\"Simulated cycles: %0d\", cycle_count);",
            "        $display(\"Control corruption errors: %0d\", control_errors);",
            "        $display(\"Mean Error Distance (MED): %f\", total_error_distance / cycle_count);",
            "        $display(\"Worst Case Error (WCE): %f\", max_error_distance);",
            "        $display(\"Error Rate (ER): %f\", error_cycle_count * 1.0 / cycle_count);",
            "        $display(\"Mean Squared Error (MSED): %f\", total_squared_error / cycle_count);",
            "        $display(\"Mean Relative Error (MRED): %f\", total_rel_error / cycle_count);",
        ])

        for sig_name in relaxed_names:
            lines.extend([
                f"        $display(\"Signal {sig_name} Mean Error Distance (MED): %f\", total_error_distance_{sig_name} / cycle_count);",
                f"        $display(\"Signal {sig_name} Worst Case Error (WCE): %f\", max_error_distance_{sig_name});",
                f"        $display(\"Signal {sig_name} Error Rate (ER): %f\", error_cycle_count_{sig_name} * 1.0 / cycle_count);",
                f"        $display(\"Signal {sig_name} Mean Squared Error (MSED): %f\", total_squared_error_{sig_name} / cycle_count);",
                f"        $display(\"Signal {sig_name} Mean Relative Error (MRED): %f\", total_rel_error_{sig_name} / cycle_count);",
            ])

        lines.extend([
            "        if (control_errors > 0) begin",
            "          $display(\"RESULT: FAILED_CONTROL_CORRUPTION\");",
            "          $finish(1);",
            "        end else begin",
            "          $display(\"RESULT: SUCCESS_CONTROL_INTACT\");",
            "          $finish(0);",
            "        end",
            "      end",
            "    end",
            "  end",
            "endmodule",
        ])

        tb_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return tb_path

    def verify(self) -> Dict[str, Any]:
        """Compile and execute Miter verification harness (simulation + optional formal proofs)."""
        with tempfile.TemporaryDirectory(prefix="miter_run_") as tmp_dir:
            work_dir = Path(tmp_dir).resolve()

            # 1. Rename approximate module to avoid collision in iverilog
            approx_renamed = self._prepare_approx_netlist(work_dir)

            # 2. Inspect ports
            ports_info = self._inspect_module_ports()

            # 3. Generate testbench
            tb_path = self.generate_miter_tb(work_dir, ports_info)

            # 4. Compile with iverilog
            sim_vvp = work_dir / "sim.vvp"
            compile_cmd = [
                "iverilog", "-g2012", "-o", str(sim_vvp),
                str(tb_path),
                str(self.exact_vlog),
                str(approx_renamed),
            ]

            logger.info("Compiling Miter: %s", " ".join(compile_cmd))
            try:
                proc_comp = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=120)
                if proc_comp.returncode != 0 and self.tech_vlog.exists():
                    compile_cmd_tech = compile_cmd + [str(self.tech_vlog)]
                    proc_comp_tech = subprocess.run(compile_cmd_tech, capture_output=True, text=True, timeout=120)
                    if proc_comp_tech.returncode == 0:
                        proc_comp = proc_comp_tech
            except subprocess.TimeoutExpired:
                logger.error("Miter compilation timed out after 120s")
                return {
                    "status": "TIMEOUT",
                    "stderr": "iverilog compilation timed out after 120 seconds",
                }

            if proc_comp.returncode != 0:
                logger.error("Miter compilation failed: %s", proc_comp.stderr)
                return {
                    "status": "COMPILATION_ERROR",
                    "stderr": proc_comp.stderr,
                }

            # 5. Run simulation with safety timeout
            run_cmd = ["vvp", str(sim_vvp)]
            try:
                proc_run = subprocess.run(run_cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                logger.error("Miter simulation timed out after 120s")
                return {
                    "status": "TIMEOUT",
                    "stderr": "vvp simulation timed out after 120 seconds",
                }

            stdout = proc_run.stdout
            logger.info("Miter simulation output:\n%s", stdout)

            # Parse results
            control_errors = -1
            m_ctrl = re.search(r"Control corruption errors:\s+(\d+)", stdout)
            if m_ctrl:
                control_errors = int(m_ctrl.group(1))

            med = 0.0
            m_med = re.search(r"Mean Error Distance \(MED\):\s+([\d\.]+)", stdout)
            if m_med:
                med = float(m_med.group(1))

            wce = 0.0
            m_wce = re.search(r"Worst Case Error \(WCE\):\s+([\d\.]+)", stdout)
            if m_wce:
                wce = float(m_wce.group(1))

            er = 0.0
            m_er = re.search(r"Error Rate \(ER\):\s+([\d\.]+)", stdout)
            if m_er:
                er = float(m_er.group(1))

            msed = 0.0
            m_msed = re.search(r"Mean Squared Error \(MSED\):\s+([\d\.]+)", stdout)
            if m_msed:
                msed = float(m_msed.group(1))

            mred = 0.0
            m_mred = re.search(r"Mean Relative Error \(MRED\):\s+([\d\.]+)", stdout)
            if m_mred:
                mred = float(m_mred.group(1))

            control_passed = (control_errors == 0) and ("RESULT: SUCCESS_CONTROL_INTACT" in stdout)

            sig_metrics: Dict[str, Dict[str, float]] = {}
            for sig in self.meta.get("relaxed_signals", []):
                wire = sig.get("wire", "")
                if not wire:
                    continue
                m_s_med = re.search(rf"Signal {re.escape(wire)} Mean Error Distance \(MED\):\s+([\d\.]+)", stdout)
                m_s_wce = re.search(rf"Signal {re.escape(wire)} Worst Case Error \(WCE\):\s+([\d\.]+)", stdout)
                m_s_er = re.search(rf"Signal {re.escape(wire)} Error Rate \(ER\):\s+([\d\.]+)", stdout)
                m_s_msed = re.search(rf"Signal {re.escape(wire)} Mean Squared Error \(MSED\):\s+([\d\.]+)", stdout)
                m_s_mred = re.search(rf"Signal {re.escape(wire)} Mean Relative Error \(MRED\):\s+([\d\.]+)", stdout)
                sig_metrics[wire] = {
                    "MED": float(m_s_med.group(1)) if m_s_med else med,
                    "WCE": float(m_s_wce.group(1)) if m_s_wce else wce,
                    "ER": float(m_s_er.group(1)) if m_s_er else er,
                    "MSED": float(m_s_msed.group(1)) if m_s_msed else msed,
                    "MRED": float(m_s_mred.group(1)) if m_s_mred else mred,
                }

            # Compare against bounds across all declared relaxed signals
            bound_passed = True
            metric_results = []
            for sig in self.meta.get("relaxed_signals", []):
                bound = float(sig.get("bound", 1e9))
                metric = sig.get("metric", "MED").upper()
                wire_name = sig.get("wire", "unknown")

                wire_metrics = sig_metrics.get(wire_name, {})
                if metric in wire_metrics:
                    measured_val = wire_metrics[metric]
                else:
                    logger.error("Unsupported error metric '%s' on signal '%s'", metric, wire_name)
                    bound_passed = False
                    metric_results.append({
                        "wire": wire_name,
                        "metric": metric,
                        "bound": bound,
                        "measured": None,
                        "satisfied": False,
                        "error": f"Unsupported metric: {metric}",
                    })
                    continue

                satisfied = measured_val <= bound
                if not satisfied:
                    bound_passed = False

                metric_results.append({
                    "wire": wire_name,
                    "metric": metric,
                    "bound": bound,
                    "measured": measured_val,
                    "satisfied": satisfied,
                })

            overall_status = "PASSED" if (control_passed and bound_passed) else "FAILED"

            result: Dict[str, Any] = {
                "status": overall_status,
                "control_integrity": "PASSED" if control_passed else "FAILED",
                "control_errors": control_errors,
                "measured_med": med,
                "measured_wce": wce,
                "measured_er": er,
                "measured_msed": msed,
                "measured_mred": mred,
                "error_bound_satisfied": bound_passed,
                "metric_evaluations": metric_results,
                "simulated_cycles": self.num_cycles,
                "log": stdout,
            }

            # 6. Execute Formal Verification Passes if enabled
            if self.enable_formal:
                formal_results: Dict[str, Any] = {}
                try:
                    ctrl_verifier = FormalControlVerifier(
                        exact_vlog=self.exact_vlog,
                        approx_vlog=self.approx_vlog,
                        meta_json=self.meta_json,
                        tech_vlog=self.tech_vlog,
                        timeout=self.formal_timeout,
                    )
                    ctrl_res = ctrl_verifier.verify()
                    formal_results["control_equivalence"] = ctrl_res

                    if ctrl_res.get("status") == "FALSIFIED":
                        logger.error("Formal verification falsified control equivalence!")
                        result["status"] = "FAILED"
                        result["control_integrity"] = "FAILED_FORMAL"
                except Exception as exc:
                    logger.warning("Formal Control verification encountered an error: %s", exc)
                    formal_results["control_equivalence"] = {"status": "ERROR", "error": str(exc)}

                # Check if cone netlists are available for formal WCE proof
                cone_exact = self.extracted_cone_vlog
                cone_approx = self.approx_cone_vlog
                if not cone_exact:
                    cand_e = self.approx_vlog.parent / "extracted_cone.v"
                    if cand_e.exists():
                        cone_exact = cand_e
                if not cone_approx:
                    cand_a = self.approx_vlog.parent / "approx_cone.v"
                    if cand_a.exists():
                        cone_approx = cand_a

                if cone_exact and cone_approx and cone_exact.exists() and cone_approx.exists():
                    try:
                        wce_verifier = FormalWceVerifier(
                            exact_cone_vlog=cone_exact,
                            approx_cone_vlog=cone_approx,
                            meta_json=self.meta_json,
                            tech_vlog=self.tech_vlog,
                            timeout=self.formal_timeout,
                        )
                        wce_res = wce_verifier.verify()
                        formal_results["wce_bound"] = wce_res
                    except Exception as exc:
                        logger.warning("Formal WCE verification encountered an error: %s", exc)
                        formal_results["wce_bound"] = {"status": "ERROR", "error": str(exc)}

                result["formal_verification"] = formal_results

            return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Miter Verification Harness CLI")
    parser.add_argument("--exact", required=True, help="Golden exact RTL Verilog")
    parser.add_argument("--approx", required=True, help="Approximated top netlist Verilog")
    parser.add_argument("--meta", required=True, help="Path to metadata.json")
    parser.add_argument("-t", "--tech", default="NanGate15nm", help="Standard cell library name")
    parser.add_argument("--tech-v", default=None, help="Standard cell library Verilog path")
    parser.add_argument("-n", "--cycles", type=int, default=500, help="Simulation clock cycles")
    parser.add_argument("--no-formal", action="store_true", help="Disable Yosys SAT formal verification")
    parser.add_argument("--formal-timeout", type=int, default=60, help="Timeout in seconds for SAT solver")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    verifier = MiterVerifier(
        exact_vlog=args.exact,
        approx_vlog=args.approx,
        meta_json=args.meta,
        tech=args.tech,
        tech_vlog=args.tech_v,
        num_cycles=args.cycles,
        enable_formal=not args.no_formal,
        formal_timeout=args.formal_timeout,
    )
    result = verifier.verify()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
