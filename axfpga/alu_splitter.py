"""Arithmetic Carry-Chain Protection and 4-Bit Nibble Splitting for AxFPGA (Plan 1.1).

Pre-processes RTL designs to prevent Yosys `techmap; abc -lut 4` from shattering
dedicated arithmetic `$alu` (CARRY4) chains into inefficient soft-LUT trees.

Key Capabilities:
1. Pre-YosysHelper Arithmetic Extraction: Identifies `$alu` cells with bit-width W >= 8.
2. 4-Bit Nibble-Aligned Splitting: Partitions wide `$alu` cells at 4k-bit boundaries:
   - Upper (W - 4k) MSBs are wrapped into a protected exact `$alu` module
     with keep_hierarchy / blackbox attributes to guarantee hard CARRY4 inference in Vivado.
   - Lower 4k LSBs are exposed for pyALS K-LUT catalog approximation or carry truncation.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pyosys import libyosys as ys

logger = logging.getLogger("axfpga.alu_splitter")


class AluSplitter:
    """Partitions and protects arithmetic carry chains on 4-bit CARRY4 nibble boundaries."""

    def __init__(
        self,
        min_width: int = 8,
        approx_nibble_bits: int = 4,
        constant_ci: bool = False,
    ):
        """
        Args:
            min_width: Minimum ALU width to trigger carry-chain protection (default >= 8).
            approx_nibble_bits: Number of LSB bits to expose (must be multiple of 4, e.g. 4 or 8).
            constant_ci: If True, tie carry-in to MSB adder to 1'b0 (carry-chain truncation).
        """
        self.min_width = min_width
        self.approx_nibble_bits = approx_nibble_bits
        self.constant_ci = constant_ci
        if self.approx_nibble_bits % 4 != 0:
            raise ValueError(f"approx_nibble_bits must be a multiple of 4 (got {self.approx_nibble_bits})")

    def identify_wide_alus(self, module: ys.Module) -> List[Tuple[ys.Cell, int]]:
        """Identify all $alu cells with bit-width W >= min_width."""
        wide_alus = []
        for cell in module.selected_cells():
            if cell.type.str() in ("$alu", r"\$alu"):
                w_param = cell.getParam(ys.IdString(r"\Y_WIDTH"))
                w = w_param.as_int() if hasattr(w_param, "as_int") else int(w_param.as_string())
                if w >= self.min_width:
                    wide_alus.append((cell, w))
        return wide_alus

    def create_exact_msb_module(
        self,
        design: ys.Design,
        msb_mod_name: str,
        msb_width: int,
        a_signed: int = 0,
        b_signed: int = 0,
        as_blackbox: bool = True,
    ) -> ys.Module:
        """Create a dedicated exact MSB adder module."""
        mod_id = ys.IdString(rf"\{msb_mod_name}")
        if mod_id in design.modules_:
            return design.modules_[mod_id]

        msb_mod = design.addModule(mod_id)
        msb_mod.set_bool_attribute(ys.IdString(r"\keep_hierarchy"), True)
        if as_blackbox:
            msb_mod.set_bool_attribute(ys.IdString(r"\blackbox"), True)

        # Ports
        w_A = msb_mod.addWire(ys.IdString(r"\A"), msb_width)
        w_A.port_input = True
        w_B = msb_mod.addWire(ys.IdString(r"\B"), msb_width)
        w_B.port_input = True
        w_CI = msb_mod.addWire(ys.IdString(r"\CI"), 1)
        w_CI.port_input = True
        w_BI = msb_mod.addWire(ys.IdString(r"\BI"), 1)
        w_BI.port_input = True

        w_Y = msb_mod.addWire(ys.IdString(r"\Y"), msb_width)
        w_Y.port_output = True
        w_X = msb_mod.addWire(ys.IdString(r"\X"), msb_width)
        w_X.port_output = True
        w_CO = msb_mod.addWire(ys.IdString(r"\CO"), msb_width)
        w_CO.port_output = True

        msb_mod.fixup_ports()
        return msb_mod

    def split_wide_alu(
        self,
        design: ys.Design,
        module: ys.Module,
        alu_cell: ys.Cell,
        total_width: int,
        idx: int = 0,
        as_blackbox: bool = True,
    ) -> Tuple[ys.Cell, ys.Cell]:
        """Split a single $alu cell into a lower 4k LSB ALU and an exact MSB module instance."""
        k_bits = min(self.approx_nibble_bits, total_width - 4)
        if k_bits <= 0:
            k_bits = 4
        msb_width = total_width - k_bits

        cell_name = alu_cell.name.str().lstrip(r"\\$").replace("$", "_")
        msb_mod_name = f"exact_alu_msb_{msb_width}b_{cell_name}_{idx}"

        a_signed_param = alu_cell.getParam(ys.IdString(r"\A_SIGNED"))
        b_signed_param = alu_cell.getParam(ys.IdString(r"\B_SIGNED"))
        a_signed = a_signed_param.as_int() if hasattr(a_signed_param, "as_int") else 0
        b_signed = b_signed_param.as_int() if hasattr(b_signed_param, "as_int") else 0

        self.create_exact_msb_module(
            design=design,
            msb_mod_name=msb_mod_name,
            msb_width=msb_width,
            a_signed=a_signed,
            b_signed=b_signed,
            as_blackbox=as_blackbox,
        )

        orig_A = ys.SigSpec(alu_cell.getPort(ys.IdString(r"\A")))
        orig_B = ys.SigSpec(alu_cell.getPort(ys.IdString(r"\B")))
        orig_CI = alu_cell.getPort(ys.IdString(r"\CI"))
        orig_BI = alu_cell.getPort(ys.IdString(r"\BI"))
        orig_Y = ys.SigSpec(alu_cell.getPort(ys.IdString(r"\Y")))
        orig_X = ys.SigSpec(alu_cell.getPort(ys.IdString(r"\X")))
        orig_CO = ys.SigSpec(alu_cell.getPort(ys.IdString(r"\CO")))

        # Normalize port widths to total_width before slicing (handles asymmetric ALUs)
        if orig_A.size() < total_width:
            orig_A.extend_u0(total_width, bool(a_signed))
        elif orig_A.size() > total_width:
            orig_A = orig_A.extract(0, total_width)

        if orig_B.size() < total_width:
            orig_B.extend_u0(total_width, bool(b_signed))
        elif orig_B.size() > total_width:
            orig_B = orig_B.extract(0, total_width)

        if orig_Y.size() < total_width:
            orig_Y.extend_u0(total_width, False)
        elif orig_Y.size() > total_width:
            orig_Y = orig_Y.extract(0, total_width)

        if orig_X.size() < total_width:
            orig_X.extend_u0(total_width, False)
        elif orig_X.size() > total_width:
            orig_X = orig_X.extract(0, total_width)

        if orig_CO.size() < total_width:
            orig_CO.extend_u0(total_width, False)
        elif orig_CO.size() > total_width:
            orig_CO = orig_CO.extract(0, total_width)
        orig_X = alu_cell.getPort(ys.IdString(r"\X"))
        orig_CO = alu_cell.getPort(ys.IdString(r"\CO"))

        # 1. Lower LSB ALU (width = k_bits)
        carry_lsb = module.addWire(ys.IdString(rf"\carry_lsb_{idx}"), k_bits)
        alu_lsb = module.addCell(ys.IdString(rf"\alu_lsb_{idx}"), ys.IdString("$alu"))
        alu_lsb.setParam(ys.IdString(r"\A_SIGNED"), ys.Const(a_signed, 1))
        alu_lsb.setParam(ys.IdString(r"\B_SIGNED"), ys.Const(b_signed, 1))
        alu_lsb.setParam(ys.IdString(r"\A_WIDTH"), ys.Const(k_bits, 32))
        alu_lsb.setParam(ys.IdString(r"\B_WIDTH"), ys.Const(k_bits, 32))
        alu_lsb.setParam(ys.IdString(r"\Y_WIDTH"), ys.Const(k_bits, 32))

        alu_lsb.setPort(ys.IdString(r"\A"), orig_A.extract(0, k_bits))
        alu_lsb.setPort(ys.IdString(r"\B"), orig_B.extract(0, k_bits))
        alu_lsb.setPort(ys.IdString(r"\CI"), orig_CI)
        alu_lsb.setPort(ys.IdString(r"\BI"), orig_BI)
        alu_lsb.setPort(ys.IdString(r"\Y"), orig_Y.extract(0, k_bits))
        alu_lsb.setPort(ys.IdString(r"\X"), orig_X.extract(0, k_bits))
        alu_lsb.setPort(ys.IdString(r"\CO"), ys.SigSpec(carry_lsb))

        # 2. Upper MSB instance (width = msb_width)
        inst_msb = module.addCell(ys.IdString(rf"\inst_exact_alu_msb_{idx}"), ys.IdString(rf"\{msb_mod_name}"))
        inst_msb.setPort(ys.IdString(r"\A"), orig_A.extract(k_bits, msb_width))
        inst_msb.setPort(ys.IdString(r"\B"), orig_B.extract(k_bits, msb_width))

        # Carry in to MSB
        if self.constant_ci:
            inst_msb.setPort(ys.IdString(r"\CI"), ys.SigSpec(ys.State.S0, 1))
        else:
            inst_msb.setPort(ys.IdString(r"\CI"), ys.SigSpec(carry_lsb, k_bits - 1, 1))

        inst_msb.setPort(ys.IdString(r"\BI"), orig_BI)
        inst_msb.setPort(ys.IdString(r"\Y"), orig_Y.extract(k_bits, msb_width))
        inst_msb.setPort(ys.IdString(r"\X"), orig_X.extract(k_bits, msb_width))
        inst_msb.setPort(ys.IdString(r"\CO"), orig_CO.extract(k_bits, msb_width))

        # Remove the original unpartitioned ALU
        module.remove(alu_cell)

        logger.info(
            "Split ALU '%s' (W=%d) at 4-bit nibble boundary -> LSB: %d-bit ($alu), MSB: %d-bit (%s, protected)",
            cell_name, total_width, k_bits, msb_width, msb_mod_name,
        )
        return alu_lsb, inst_msb

    def protect_and_split(
        self,
        design: ys.Design,
        top_module_name: Optional[str] = None,
        as_blackbox: bool = True,
    ) -> int:
        """Run proc; opt; alumacc and split all wide $alu cells in the top module."""
        # Step 1: Pre-YosysHelper arithmetic extraction
        ys.run_pass("proc; opt; alumacc", design)

        top_mod = design.top_module() if top_module_name is None else design.module(ys.IdString(rf"\{top_module_name}"))
        if top_mod is None:
            for mod in design.selected_whole_modules_warn():
                top_mod = mod
                break

        if top_mod is None:
            logger.warning("No modules found in design to partition ALUs.")
            return 0

        # Step 2: Identify wide ALUs
        wide_alus = self.identify_wide_alus(top_mod)
        logger.info("Identified %d wide $alu cells (W >= %d) in %s", len(wide_alus), self.min_width, top_mod.name.str())

        split_count = 0
        for i, (alu_cell, width) in enumerate(wide_alus):
            self.split_wide_alu(
                design=design,
                module=top_mod,
                alu_cell=alu_cell,
                total_width=width,
                idx=i,
                as_blackbox=as_blackbox,
            )
            split_count += 1

        return split_count

    @staticmethod
    def unblackbox_exact_modules(design: ys.Design) -> None:
        """Remove blackbox attributes from exact ALU MSB modules prior to sign-off synthesis."""
        for mod_name, mod in list(design.modules_.items()):
            name_str = mod_name.str()
            if "exact_alu_msb" in name_str:
                mod.set_bool_attribute(ys.IdString(r"\blackbox"), False)
                # Ensure keep_hierarchy is preserved for Vivado CARRY4 packing
                mod.set_bool_attribute(ys.IdString(r"\keep_hierarchy"), True)
                logger.info("Unblackboxed exact MSB module '%s' for final synthesis", name_str)

    @staticmethod
    def fixup_verilog_code(verilog_code: str) -> str:
        """Replace internal \\$alu cell instantiations and empty stubs in exact ALU MSB modules with synthesizable behavioral Verilog."""
        import re

        pattern = re.compile(
            r"(module\s+(\\?exact_alu_msb_(\d+)b(?:_[^;\s(]+)?)\s*\([^)]*\);)"
            r"(.*?)"
            r"(endmodule)",
            re.DOTALL,
        )

        def replacer(m: re.Match) -> str:
            header = m.group(1)
            msb_width = int(m.group(3))
            body = m.group(4)
            endmodule = m.group(5)

            # Remove any internal \$alu cell instantiation or dummy assigns
            body = re.sub(r"\\\$alu\s*#\(.*?\)\s*alu_msb_core\s*\([^;]*\);", "", body, flags=re.DOTALL)
            body = re.sub(r"\s*assign\s+(?:CO|X|Y)\s*=\s*[^;]+;", "", body)

            assign_code = (
                f"\n  assign {{CO[{msb_width - 1}], Y}} = BI ? ({{1'b0, A}} - {{1'b0, B}} - CI) : ({{1'b0, A}} + {{1'b0, B}} + CI);\n"
                f"  assign X = A ^ B;\n"
            )
            if msb_width > 1:
                assign_code += f"  assign CO[{msb_width - 2}:0] = {msb_width - 1}'b0;\n"

            return f"{header}{body}{assign_code}{endmodule}"

        result = pattern.sub(replacer, verilog_code)

        # Fallback: if instances exist but module definitions were omitted by Yosys (e.g. if blackboxed)
        inst_pattern = re.compile(r"\b(exact_alu_msb_(\d+)b(?:_[a-zA-Z0-9_$]+)?)\s+\w+\s*\(")
        seen_mods = set(re.findall(r"module\s+(\\?exact_alu_msb_\w+)", result))
        missing_mods = {}
        for inst_match in inst_pattern.finditer(result):
            inst_type = inst_match.group(1)
            w = int(inst_match.group(2))
            clean_name = inst_type.lstrip("\\")
            if clean_name not in seen_mods and ("\\" + clean_name) not in seen_mods and clean_name not in missing_mods:
                missing_mods[clean_name] = w

        for mod_name, msb_width in missing_mods.items():
            co_lsb = f"  assign CO[{msb_width - 2}:0] = {msb_width - 1}'b0;\n" if msb_width > 1 else ""
            mod_def = (
                f"\n(* keep_hierarchy = 1 *)\n"
                f"module {mod_name}(A, B, BI, CI, CO, X, Y);\n"
                f"  input [{msb_width - 1}:0] A;\n"
                f"  input [{msb_width - 1}:0] B;\n"
                f"  input BI;\n"
                f"  input CI;\n"
                f"  output [{msb_width - 1}:0] CO;\n"
                f"  output [{msb_width - 1}:0] X;\n"
                f"  output [{msb_width - 1}:0] Y;\n"
                f"  assign {{CO[{msb_width - 1}], Y}} = BI ? ({{1'b0, A}} - {{1'b0, B}} - CI) : ({{1'b0, A}} + {{1'b0, B}} + CI);\n"
                f"  assign X = A ^ B;\n"
                f"{co_lsb}"
                f"endmodule\n"
            )
            result += mod_def

        return result

    @classmethod
    def fixup_verilog_file(cls, netlist_path: str | Path) -> None:
        """Fixup an exported Verilog file in place to ensure exact ALU MSB modules are cleanly synthesizable."""
        p = Path(netlist_path)
        if p.exists() and p.is_file():
            content = p.read_text(encoding="utf-8")
            fixed = cls.fixup_verilog_code(content)
            if fixed != content:
                p.write_text(fixed, encoding="utf-8")
                logger.info("Fixed up exact ALU MSB module in %s for sign-off synthesis", p.name)

    @classmethod
    def generate_loa_candidates(
        cls,
        cone_content: str,
        iface: Dict[str, Any],
        bound: float,
        work_dir: Path,
    ) -> List[Dict[str, Any]]:
        """Generate Lower-part OR Adder (LOA) candidates for structural/behavioral adders and subtractors.
        
        Part of AxFPGA Plan 2 (Pillar 1).
        Replaces the lower k bits of sum with bitwise OR: S[k-1:0] = A[k-1:0] | B[k-1:0]
        and severs the carry propagation into the upper exact adder (CI = 1'b0).
        """
        import math
        import subprocess

        candidates: List[Dict[str, Any]] = []
        inputs = iface.get("inputs", {})
        outputs = iface.get("outputs", {})
        rel_out = iface.get("relaxed_output")
        top_name = iface.get("module_name", "top")

        if not rel_out or rel_out not in outputs:
            return candidates

        # Find operand inputs with bit width >= 4
        operand_inputs = [
            (name, info) for name, info in inputs.items()
            if info["width"] >= 4 and not name.lower().startswith(("\\clk", "\\rst", "clk", "rst", "\\cin", "cin"))
        ]
        if len(operand_inputs) < 2:
            return candidates

        # Use first two multi-bit inputs as operands
        in1_name, in1_info = operand_inputs[0]
        in2_name, in2_info = operand_inputs[1]
        out_w = outputs[rel_out]["width"]
        in1_lsb = in1_info.get("lsb", 0)
        in1_msb = in1_info.get("msb", in1_info["width"] - 1)
        in2_lsb = in2_info.get("lsb", 0)
        in2_msb = in2_info.get("msb", in2_info["width"] - 1)
        rel_lsb = outputs[rel_out].get("lsb", 0)
        rel_msb = outputs[rel_out].get("msb", outputs[rel_out]["width"] - 1)

        base_lsb = max(in1_lsb, in2_lsb, rel_lsb)
        base_msb = min(in1_msb, in2_msb)
        op_w = base_msb - base_lsb + 1

        # Determine safe depth horizon k based on bound: (2^k - 1) * 2^(base_lsb) <= bound
        effective_bound = bound / (1 << base_lsb) if (1 << base_lsb) <= bound else bound
        safe_k = int(math.floor(math.log2(max(1.0, effective_bound) + 1.0 + 1e-9)))
        safe_k = min(safe_k, op_w - 2)
        if safe_k < 1:
            return candidates

        is_sub = "sub" in top_name.lower() or "diff" in rel_out.lower()
        depths_to_test = sorted(set([k for k in [1, 2, 4, 6, 8, 10, safe_k] if 1 <= k <= safe_k]))

        def fmt_id(s: str) -> str:
            s_str = str(s).strip()
            return f"{s_str} " if s_str.startswith("\\") and not s_str.endswith(" ") else s_str

        fid_in1 = fmt_id(in1_name)
        fid_in2 = fmt_id(in2_name)
        fid_rel = fmt_id(rel_out)

        for k in depths_to_test:
            cand_raw_v = work_dir / f"cand_pyals_loa_{k}b_raw.v"
            cand_opt_v = work_dir / f"cand_pyals_loa_{k}b.v"

            # Construct port declarations preserving declared bus bounds
            inp_decls = []
            for name, info in inputs.items():
                w = info["width"]
                msb = info.get("msb", w - 1)
                lsb = info.get("lsb", 0)
                fid = fmt_id(name)
                inp_decls.append(f"  input [{msb}:{lsb}] {fid};" if w > 1 else f"  input {fid};")

            out_decls = []
            for name, info in outputs.items():
                w = info["width"]
                msb = info.get("msb", w - 1)
                lsb = info.get("lsb", 0)
                fid = fmt_id(name)
                out_decls.append(f"  output [{msb}:{lsb}] {fid};" if w > 1 else f"  output {fid};")

            msb_w = op_w - k
            extra_assigns = []

            # Handle auxiliary outputs (e.g. prefix adder bit 0 \U0.U0.U0.Po and carry out \U0.U0.U64.Go)
            for name, info in outputs.items():
                if name != rel_out:
                    w = info["width"]
                    o_lsb = info.get("lsb", 0)
                    o_msb = info.get("msb", w - 1)
                    fid = fmt_id(name)
                    clean_lower = name.lower()
                    if w == out_w:
                        extra_assigns.append(f"  assign {fid} = {fid_rel};")
                    elif clean_lower.endswith(".s") or clean_lower == "s":
                        extra_assigns.append(f"  assign {fid} = {fid_rel}[{o_msb}:{o_lsb}];")
                    elif w == 1 and ("po" in clean_lower or "p0" in clean_lower):
                        if is_sub:
                            extra_assigns.append(f"  assign {fid} = {fid_in1}[{base_lsb}] ^ {fid_in2}[{base_lsb}];")
                        else:
                            extra_assigns.append(f"  assign {fid} = {fid_rel}[{base_lsb}];")
                    elif w == 1 and ("go" in clean_lower or "g0" in clean_lower or "cout" in clean_lower or "co" in clean_lower):
                        if is_sub:
                            extra_assigns.append(f"  assign {fid} = 1'b0;")
                        else:
                            extra_assigns.append(f"  assign {fid} = msb_sum[{msb_w}];")
                    else:
                        m_i = re.search(r"U(\d+)\.I", name)
                        m_o = re.search(r"U(\d+)\.O", name)
                        if m_i:
                            b = int(m_i.group(1))
                            extra_assigns.append(f"  assign {fid} = {fid_rel}[{b}];")
                        elif m_o:
                            if w == 1:
                                extra_assigns.append(f"  assign {fid} = ~ {fid_rel}[{o_lsb}];")
                            else:
                                for i in range(w):
                                    bit = o_lsb + i
                                    if i == 0:
                                        extra_assigns.append(f"  assign {fid}[{bit}] = ~ {fid_rel}[{bit}];")
                                    elif i == w - 1:
                                        all_or = " | ".join(f"{fid_rel}[{o_lsb+j}]" for j in range(i))
                                        extra_assigns.append(f"  assign {fid}[{bit}] = ~ ({all_or});")
                                    else:
                                        prev_or = " | ".join(f"{fid_rel}[{o_lsb+j}]" for j in range(i))
                                        extra_assigns.append(f"  assign {fid}[{bit}] = ~ {fid_rel}[{bit}] ^ ({prev_or});")
                        else:
                            extra_assigns.append(f"  assign {fid} = {w}'b0;")

            if is_sub:
                calc_code = (
                    f"  wire [{op_w-1}:0] in1_approx = {{{fid_in1}[{base_msb}:{base_lsb+k}], {k}'b0}};\n"
                    f"  wire [{op_w-1}:0] in2_approx = {{{fid_in2}[{base_msb}:{base_lsb+k}], {k}'b0}};\n"
                    f"  wire [{op_w-1}:0] diff_approx = in1_approx - in2_approx;\n"
                    f"  assign {fid_rel}[{base_msb}:{base_lsb}] = diff_approx;\n"
                )
            else:
                carry_ext = f"\n  assign {fid_rel}[{rel_msb}] = msb_sum[{msb_w}];" if rel_msb > base_msb else ""
                calc_code = (
                    f"  wire [{msb_w}:0] msb_sum = {fid_in1}[{base_msb}:{base_lsb+k}] + {fid_in2}[{base_msb}:{base_lsb+k}];\n"
                    f"  assign {fid_rel}[{base_lsb}] = {fid_in1}[{base_lsb}] | {fid_in2}[{base_lsb}];\n"
                )
                if k > 1:
                    calc_code += f"  assign {fid_rel}[{base_lsb+k-1}:{base_lsb+1}] = {fid_in1}[{base_lsb+k-1}:{base_lsb+1}] | {fid_in2}[{base_lsb+k-1}:{base_lsb+1}];\n"
                calc_code += f"  assign {fid_rel}[{base_msb}:{base_lsb+k}] = msb_sum[{msb_w-1}:0];{carry_ext}\n"

            if rel_lsb < base_lsb:
                calc_code += f"  assign {fid_rel}[{base_lsb-1}:{rel_lsb}] = {fid_in1}[{base_lsb-1}:{rel_lsb}];\n"

            header_ports = ", ".join([fmt_id(p) for p in list(inputs.keys()) + list(outputs.keys())])
            mod_code = (
                f"`timescale 1ns/1ps\n"
                f"module {top_name} (\n"
                f"  {header_ports}\n"
                f");\n"
                f"{chr(10).join(inp_decls)}\n"
                f"{chr(10).join(out_decls)}\n\n"
                f"{chr(10).join(extra_assigns)}\n"
                f"{calc_code}\n"
                f"endmodule\n"
            )

            cand_raw_v.write_text(mod_code, encoding="utf-8")

            # Clean and optimize candidate
            ys_cmd = (
                f"read_verilog -sv {cand_raw_v}; "
                f"hierarchy -check -top {top_name}; "
                f"proc; opt_clean -purge; opt_expr; opt_merge; clean; "
                f"write_verilog -noattr {cand_opt_v}"
            )
            res = subprocess.run(f"yosys -q -p \"{ys_cmd}\"", shell=True, executable="/bin/bash", capture_output=True, text=True)
            final_cand = cand_opt_v if (res.returncode == 0 and cand_opt_v.exists()) else cand_raw_v

            candidates.append({
                "name": f"pyALS_loa_{k}b",
                "path": final_cand,
                "strategy": "pyals",
                "method": "loa_adder",
                "depth": k,
                "theoretical_wce": (1 << k) - 1,
            })
            logger.info("Generated LOA candidate 'pyALS_loa_%db' (theoretical WCE=%d <= bound %.1f)",
                        k, (1 << k) - 1, bound)

        return candidates
