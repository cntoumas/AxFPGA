"""FPGA Arithmetic Compressors for LUT6 Architectures (Xilinx 7-Series / Artix-7).

Provides exact and approximate 4:2 and 5:3 compressors mapped directly to single
fracturable 6-input LUT primitives (LUT6_2 / LUT6).

In Xilinx 7-Series slices, a physical LUT6 can operate as two 5-input LUTs (O5, O6)
sharing the same 5 inputs (I0..I4) with I5=1'b1. This allows:
1. Exact & Approximate 4:2 compressors (4 inputs + carry-in -> Sum, Carry)
   mapped to a SINGLE physical LUT6 with ZERO CARRY4 carry chains.
2. Approximate 5:3 / 5:2 compressors (5 inputs -> Sum, Carry)
   mapped to a SINGLE physical LUT6 with ZERO CARRY4 carry chains.
3. 2-Bit Dual-Sum Addition Slices (A0, B0, Cin, A1, B1 -> Sum0, Sum1)
   mapped to a SINGLE physical LUT6 with ZERO CARRY4 carry chains.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("compressors")

# -------------------------------------------------------------------------
# Exact & Approximate Truth Tables (INIT strings) for LUT6_2
# -------------------------------------------------------------------------
# 4:2 Compressor (I0=x1, I1=x2, I2=x3, I3=x4, I4=cin, I5=1'b1):
#   Sum   = popcount(I0..I4) % 2          -> O5 INIT[31:0]  = 32'h96696996
#   Carry = popcount(I0..I4) >= 2         -> O6 INIT[63:32] = 32'hfffefee8
#   Combined LUT6_2 INIT: 64'hfffefee896696996
LUT6_2_COMP42_EXACT_INIT = "64'hfffefee896696996"

# Approximate 4:2 Compressor (Momeni / Dual-output, lower switching):
#   Sum   = (x1 ^ x2) | (x3 ^ x4)         -> O5 INIT[31:0]  = 32'h6ff66ff6
#   Carry = (x1 & x2) | (x3 & x4)         -> O6 INIT[63:32] = 32'hf888f888
#   Combined LUT6_2 INIT: 64'hf888f8886ff66ff6
LUT6_2_COMP42_APPROX_INIT = "64'hf888f8886ff66ff6"

# Approximate 5:3 / 5:2 Compressor (I0..I4 = 5 inputs, I5=1'b1):
#   Compresses 5 inputs into 2 outputs (Sum, Carry) in a single LUT6_2
#   Sum   = popcount(I0..I4) % 2          -> O5 INIT[31:0]  = 32'h96696996
#   Carry = popcount(I0..I4) >= 2         -> O6 INIT[63:32] = 32'hfffefee8
#   Combined LUT6_2 INIT: 64'hfffefee896696996
LUT6_2_COMP52_APPROX_INIT = "64'hfffefee896696996"

# Exact 5:3 Compressor Carry2 output (popcount >= 4 over 5 inputs):
#   Carry2 = 1 iff popcount(I0..I4) >= 4  -> INIT = 32'he8808000
LUT6_COMP53_C2_INIT = "64'h00000000e8808000"

# 2-Bit Dual-Sum Addition Slice (I0=A0, I1=B0, I2=Cin, I3=A1, I4=B1, I5=1'b1):
#   O5 = Sum0 = A0 ^ B0 ^ Cin             -> INIT[31:0]  = 32'h96969696
#   O6 = Sum1 = A1 ^ B1 ^ Carry(A0,B0,Cin)-> INIT[63:32] = 32'he81717e8
#   Combined LUT6_2 INIT: 64'he81717e896969696
LUT6_2_DUAL_ADDER_2B_INIT = "64'he81717e896969696"


COMPRESSOR_VERILOG_PRIMITIVES = """
// -----------------------------------------------------------------------------
// Exact 4:2 Compressor mapped to a SINGLE Xilinx LUT6_2 primitive
// Inputs: x1, x2, x3, x4, cin
// Outputs: sum (O5), carry (O6)
// Resource: 1 LUT6 (0 CARRY4)
// -----------------------------------------------------------------------------
(* keep *)
module lut6_compressor_4_2 (
    input  wire x1,
    input  wire x2,
    input  wire x3,
    input  wire x4,
    input  wire cin,
    output wire sum,
    output wire carry
);
    LUT6_2 #(
        .INIT(64'hfffefee896696996)
    ) comp42_inst (
        .O5(sum),
        .O6(carry),
        .I0(x1),
        .I1(x2),
        .I2(x3),
        .I3(x4),
        .I4(cin),
        .I5(1'b1)
    );
endmodule

// -----------------------------------------------------------------------------
// Approximate 4:2 Compressor mapped to a SINGLE Xilinx LUT6_2 primitive
// Simplified carry/sum with reduced switching power
// Resource: 1 LUT6 (0 CARRY4)
// -----------------------------------------------------------------------------
(* keep *)
module lut6_approx_compressor_4_2 (
    input  wire x1,
    input  wire x2,
    input  wire x3,
    input  wire x4,
    input  wire cin,
    output wire sum,
    output wire carry
);
    LUT6_2 #(
        .INIT(64'hf888f8886ff66ff6)
    ) comp42_approx_inst (
        .O5(sum),
        .O6(carry),
        .I0(x1),
        .I1(x2),
        .I2(x3),
        .I3(x4),
        .I4(cin),
        .I5(1'b1)
    );
endmodule

// -----------------------------------------------------------------------------
// Approximate 5:3 / 5:2 Compressor mapped strictly to a SINGLE LUT6_2
// Compresses 5 input bits of identical weight into 2 output bits (Sum, Carry)
// Resource: 1 LUT6 (0 CARRY4)
// -----------------------------------------------------------------------------
(* keep *)
module lut6_approx_compressor_5_3 (
    input  wire x1,
    input  wire x2,
    input  wire x3,
    input  wire x4,
    input  wire x5,
    output wire sum,
    output wire carry
);
    LUT6_2 #(
        .INIT(64'hfffefee896696996)
    ) comp52_inst (
        .O5(sum),
        .O6(carry),
        .I0(x1),
        .I1(x2),
        .I2(x3),
        .I3(x4),
        .I4(x5),
        .I5(1'b1)
    );
endmodule

// -----------------------------------------------------------------------------
// Exact 5:3 Compressor mapped to LUT6_2 (Sum, Carry1) + LUT6 (Carry2)
// Resource: 2 LUT6s (0 CARRY4)
// -----------------------------------------------------------------------------
(* keep *)
module lut6_compressor_5_3 (
    input  wire x1,
    input  wire x2,
    input  wire x3,
    input  wire x4,
    input  wire x5,
    output wire sum,
    output wire carry1,
    output wire carry2
);
    LUT6_2 #(
        .INIT(64'hfffefee896696996)
    ) comp53_lower (
        .O5(sum),
        .O6(carry1),
        .I0(x1),
        .I1(x2),
        .I2(x3),
        .I3(x4),
        .I4(x5),
        .I5(1'b1)
    );

    LUT6 #(
        .INIT(64'h00000000e8808000)
    ) comp53_c2 (
        .O(carry2),
        .I0(x1),
        .I1(x2),
        .I2(x3),
        .I3(x4),
        .I4(x5),
        .I5(1'b0)
    );
endmodule

// -----------------------------------------------------------------------------
// 2-Bit Dual-Sum Addition Slice mapped to a SINGLE Xilinx LUT6_2 primitive
// Simultaneously evaluates Sum0 and Sum1 from A0, B0, Cin, A1, B1 with ZERO CARRY4
// Resource: 1 LUT6 (0 CARRY4) for 2 output bits
// -----------------------------------------------------------------------------
(* keep *)
module lut6_dual_adder_2b (
    input  wire a0,
    input  wire b0,
    input  wire cin,
    input  wire a1,
    input  wire b1,
    output wire sum0,
    output wire sum1
);
    LUT6_2 #(
        .INIT(64'he81717e896969696)
    ) dual_add_inst (
        .O5(sum0),
        .O6(sum1),
        .I0(a0),
        .I1(b0),
        .I2(cin),
        .I3(a1),
        .I4(b1),
        .I5(1'b1)
    );
endmodule
"""


def fmt_sig(name: str) -> str:
    """Format signal name with trailing space if escaped identifier."""
    return f"{name} " if name.startswith("\\") else name


def get_compressor_definitions() -> str:
    """Return the Verilog source defining all LUT6_2 compressor modules."""
    return COMPRESSOR_VERILOG_PRIMITIVES.strip()


def generate_compressor_candidates(
    cone_content: str | Path,
    iface: Dict[str, Any],
    work_dir: Path,
) -> List[Dict[str, Any]]:
    """Generate compressor-driven approximate candidates using single-LUT6 primitives."""
    if isinstance(cone_content, Path):
        cone_content = cone_content.read_text(encoding="utf-8")
    candidates = []
    comp_defs = get_compressor_definitions()

    rel_out = iface.get("relaxed_output")
    raw_inputs = iface.get("inputs", {})
    raw_outputs = iface.get("outputs", {})

    inputs = list(raw_inputs.values()) if isinstance(raw_inputs, dict) else raw_inputs
    outputs = list(raw_outputs.values()) if isinstance(raw_outputs, dict) else raw_outputs

    # Module header from original
    mod_m = re.search(r"module\s+(\w+)\s*\((.*?)\);", cone_content, re.DOTALL)
    if not mod_m:
        return candidates

    mod_name = mod_m.group(1)
    port_list = mod_m.group(2)
    valid_ports = set(p.strip().split()[-1] for p in port_list.split(",") if p.strip())

    # Normalize inputs and outputs to list of dicts with 'name' and 'width'
    norm_inputs = []
    for inp in inputs:
        if isinstance(inp, dict):
            norm_inputs.append(inp)
        elif isinstance(inp, (list, tuple)) and len(inp) >= 2:
            norm_inputs.append({"name": inp[0], "width": inp[1]})
        elif isinstance(inp, (list, tuple)) and len(inp) == 1:
            norm_inputs.append({"name": inp[0], "width": 1})

    norm_outputs = []
    for out in outputs:
        if isinstance(out, dict):
            norm_outputs.append(out)
        elif isinstance(out, (list, tuple)) and len(out) >= 2:
            norm_outputs.append({"name": out[0], "width": out[1]})
        elif isinstance(out, (list, tuple)) and len(out) == 1:
            norm_outputs.append({"name": out[0], "width": 1})

    # Filter inputs and outputs to strictly match ports of this module
    inputs = [inp for inp in norm_inputs if not valid_ports or inp.get("name") in valid_ports]
    outputs = [out for out in norm_outputs if not valid_ports or out.get("name") in valid_ports]

    # Check for multi-bit inputs and outputs suitable for arithmetic compression
    multibit_inputs = [inp for inp in inputs if isinstance(inp, dict) and inp.get("width", 1) >= 4]
    multibit_outputs = [out for out in outputs if isinstance(out, dict) and out.get("width", 1) >= 4]

    if len(multibit_inputs) >= 2 and len(multibit_outputs) >= 1:
        in1 = multibit_inputs[0]
        in2 = multibit_inputs[1]
        out_target = multibit_outputs[0]
        w = min(in1["width"], in2["width"], out_target["width"])

        for approx_k in [2, 4]:
            if approx_k > w:
                continue

            # Candidate 1: 4:2 Compressor / Dual-Sum Slice Replacement on Lower K Bits
            cand_code = []
            cand_code.append(comp_defs)
            cand_code.append("")
            cand_code.append(f"module {mod_name}({port_list});")

            # Declare ports
            for inp in inputs:
                w_str = f"[{inp['width']-1}:0] " if inp.get("width", 1) > 1 else ""
                cand_code.append(f"  input {w_str}{fmt_sig(inp['name'])};")
            for out in outputs:
                w_str = f"[{out['width']-1}:0] " if out.get("width", 1) > 1 else ""
                cand_code.append(f"  output {w_str}{fmt_sig(out['name'])};")

            in1_n = fmt_sig(in1["name"])
            in2_n = fmt_sig(in2["name"])
            out_n = fmt_sig(out_target["name"])

            # Instantiations of single-LUT6 dual-sum addition slices for lower K bits
            cand_code.append(f"  // Lower {approx_k} bits compressed using LUT6_2 dual-sum slices (0 CARRY4)")
            num_pairs = approx_k // 2
            for p in range(num_pairs):
                b0 = 2 * p
                b1 = 2 * p + 1
                cin_sig = "1'b0" if p == 0 else f"c_mid_{p-1}"
                cand_code.append(f"  lut6_dual_adder_2b comp_pair_{p} (")
                cand_code.append(f"    .a0({in1_n}[{b0}]), .b0({in2_n}[{b0}]), .cin({cin_sig}),")
                cand_code.append(f"    .a1({in1_n}[{b1}]), .b1({in2_n}[{b1}]),")
                cand_code.append(f"    .sum0({out_n}[{b0}]), .sum1({out_n}[{b1}])")
                cand_code.append(f"  );")
                cand_code.append(f"  wire c_mid_{p} = ({in1_n}[{b1}] & {in2_n}[{b1}]) | (({in1_n}[{b1}] ^ {in2_n}[{b1}]) & ({in1_n}[{b0}] & {in2_n}[{b0}]));")

            # Upper bits addition using exact logic
            cand_code.append(f"  assign {out_n}[{w-1}:{approx_k}] = {in1_n}[{w-1}:{approx_k}] + {in2_n}[{w-1}:{approx_k}] + c_mid_{num_pairs-1};")
            cand_code.append("endmodule")

            cand_v = work_dir / f"cand_pyals_comp42_{approx_k}b.v"
            cand_v.write_text("\n".join(cand_code), encoding="utf-8")
            candidates.append({
                "name": f"pyALS_comp42_{approx_k}b",
                "path": cand_v,
                "strategy": "compressor",
                "distance": approx_k,
                "replaced_luts": num_pairs,
            })
            logger.info("Generated 4:2 compressor candidate 'pyALS_comp42_%db' using LUT6_2", approx_k)

            # Candidate 2: Approximate 5:3 / 5:2 Compressor on Lower K Bits
            cand53_code = []
            cand53_code.append(comp_defs)
            cand53_code.append("")
            cand53_code.append(f"module {mod_name}({port_list});")
            for inp in inputs:
                w_str = f"[{inp['width']-1}:0] " if inp.get("width", 1) > 1 else ""
                cand53_code.append(f"  input {w_str}{fmt_sig(inp['name'])};")
            for out in outputs:
                w_str = f"[{out['width']-1}:0] " if out.get("width", 1) > 1 else ""
                cand53_code.append(f"  output {w_str}{fmt_sig(out['name'])};")

            cand53_code.append(f"  // Lower {approx_k} bits compressed using single-LUT6 approx 5:2/5:3 compressors")
            for b in range(approx_k):
                prev_c = "1'b0" if b == 0 else f"carry53_{b-1}"
                cand53_code.append(f"  wire carry53_{b};")
                cand53_code.append(f"  lut6_approx_compressor_5_3 comp53_bit_{b} (")
                cand53_code.append(f"    .x1({in1_n}[{b}]), .x2({in2_n}[{b}]), .x3({prev_c}), .x4(1'b0), .x5(1'b0),")
                cand53_code.append(f"    .sum({out_n}[{b}]), .carry(carry53_{b})")
                cand53_code.append(f"  );")
            cand53_code.append(f"  assign {out_n}[{w-1}:{approx_k}] = {in1_n}[{w-1}:{approx_k}] + {in2_n}[{w-1}:{approx_k}] + carry53_{approx_k-1};")
            cand53_code.append("endmodule")

            cand53_v = work_dir / f"cand_pyals_comp53_{approx_k}b.v"
            cand53_v.write_text("\n".join(cand53_code), encoding="utf-8")
            candidates.append({
                "name": f"pyALS_comp53_{approx_k}b",
                "path": cand53_v,
                "strategy": "compressor",
                "distance": approx_k,
                "replaced_luts": approx_k,
            })
            logger.info("Generated 5:3 compressor candidate 'pyALS_comp53_%db' using LUT6_2", approx_k)

    return candidates
