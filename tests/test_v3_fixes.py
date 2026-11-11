"""Regressions for defects found in the explore_v3 run (job 2777047)."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from axfpga.alu_splitter import AluSplitter
from axfpga.estimator import FpgaEstimator
from axfpga.explore.evaluate import ConeEvaluator

W = 4


def _sim_alu(tmp_path, netlist: str):
    """Simulate exact_alu_msb_4b exhaustively; rows of (a, b, bi, ci, y, x, co_msb)."""
    tb = f"""module tb;
  reg [{W-1}:0] A, B; reg BI, CI; wire [{W-1}:0] CO, X, Y; integer i;
  exact_alu_msb_{W}b dut(.A(A), .B(B), .BI(BI), .CI(CI), .CO(CO), .X(X), .Y(Y));
  initial begin
    for (i = 0; i < {1 << (2 * W + 2)}; i = i + 1) begin
      {{BI, CI, A, B}} = i; #1 $display("%0d %0d %0d %0d %0d %0d %0d", A, B, BI, CI, Y, X, CO[{W-1}]);
    end
    $finish;
  end
endmodule
"""
    (tmp_path / "alu.v").write_text(netlist)
    (tmp_path / "tb.v").write_text(tb)
    subprocess.run(["iverilog", "-g2012", "-o", str(tmp_path / "a.vvp"), str(tmp_path / "tb.v"), str(tmp_path / "alu.v")],
                   check=True, capture_output=True)
    out = subprocess.run(["vvp", "-n", str(tmp_path / "a.vvp")], check=True, capture_output=True, text=True).stdout
    return [tuple(map(int, ln.split())) for ln in out.splitlines() if ln.strip() and ln[0].isdigit()]


@pytest.mark.parametrize("with_module", [False, True])
def test_alu_msb_fixup_matches_yosys_alu(tmp_path, with_module):
    """Yosys $alu: Y = A + (BI ? ~B : B) + CI, X = A ^ (BI ? ~B : B), CO[msb] = carry out (no-borrow when BI)."""
    if with_module:     # module body emitted by Yosys (stub assigns replaced)
        src = (f"module exact_alu_msb_{W}b(A, B, BI, CI, CO, X, Y);\n  input [{W-1}:0] A; input [{W-1}:0] B; input BI; "
               f"input CI;\n  output [{W-1}:0] CO; output [{W-1}:0] X; output [{W-1}:0] Y;\n  assign Y = 0;\nendmodule\n")
    else:               # instance only: the fallback module definition is appended
        src = f"module top(); exact_alu_msb_{W}b u0 (); endmodule\n"
    rows = _sim_alu(tmp_path, AluSplitter.fixup_verilog_code(src))
    assert len(rows) == 1 << (2 * W + 2)
    m = (1 << W) - 1
    for a, b, bi, ci, y, x, co in rows:
        bb = (~b & m) if bi else b
        s = a + bb + ci
        assert (y, co, x) == (s & m, s >> W, a ^ bb), (a, b, bi, ci)


def test_cone_evaluator_simulates_xilinx_primitives(tmp_path):
    """Compressor candidates instantiate LUT6_2; they used to fail to compile (no cells_sim.v) and were never scored."""
    cone = tmp_path / "cone1.v"
    cone.write_text("module cone1(a, b, y);\n  input [1:0] a;\n  input [1:0] b;\n  output [1:0] y;\n"
                    "  assign y = a ^ b;\nendmodule\n")
    ev = ConeEvaluator(cone, None, tmp_path / "ev", FpgaEstimator(family="xc7"), {"wce": 0})
    cand = tmp_path / "cand.v"
    cand.write_text("module cone1(a, b, y);\n  input [1:0] a;\n  input [1:0] b;\n  output [1:0] y;\n"
                    "  LUT2 #(.INIT(4'h6)) l0 (.O(y[0]), .I0(a[0]), .I1(b[0]));\n"
                    "  LUT6_2 #(.INIT(64'h0000000600000000)) l1 (.O5(), .O6(y[1]), .I0(a[1]), .I1(b[1]), .I2(1'b0),"
                    " .I3(1'b0), .I4(1'b0), .I5(1'b1));\nendmodule\n")
    vals = ev._simulate(cand, "prim")
    assert vals is not None
    assert np.array_equal(vals, ev.exact_vals)


def test_compressor_candidates_drive_the_carry_out(tmp_path):
    """Adder cones whose output is one bit wider than the operands: the MSB used to be left undriven (WCE 2^n)."""
    from axfpga.compressors import generate_compressor_candidates
    cone = tmp_path / "add4.v"
    cone.write_text("module add4(a, b, s);\n  input [3:0] a;\n  input [3:0] b;\n  output [4:0] s;\n"
                    "  assign s = a + b;\nendmodule\n")
    iface = {"module_name": "add4", "inputs": [("a", 4), ("b", 4)], "outputs": [("s", 5)]}
    cands = {c["name"]: Path(c["path"]) for c in generate_compressor_candidates(cone, iface, tmp_path)}
    ev = ConeEvaluator(cone, None, tmp_path / "ev", FpgaEstimator(family="xc7"), {"wce": 0})
    wce = {}
    for name, path in cands.items():
        vals = ev._simulate(path, name)
        assert vals is not None, name
        wce[name] = float(np.abs(vals - ev.exact_vals).max())
    # 2-bit dual-sum slice and the full-adder 5:3 chain are exact; 4b 4:2 speculates the carry out of the lower pair
    assert wce["pyALS_comp42_2b"] == 0 and wce["pyALS_comp53_2b"] == 0 and wce["pyALS_comp53_4b"] == 0
    assert 0 < wce["pyALS_comp42_4b"] <= 16


def test_compressor_candidates_keep_the_other_cone_outputs(tmp_path):
    """Cone outputs other than the relaxed one (precise outputs read elsewhere in the design) used to be left undriven:
    feasible at cone level, X in the reintegrated design (Kogge-Stone: WCE 65551)."""
    from axfpga.compressors import generate_compressor_candidates
    from axfpga.config import xilinx_cells_sim
    cone = tmp_path / "add4p.v"
    cone.write_text("module add4p(a, b, s, p);\n  input [3:0] a;\n  input [3:0] b;\n  output [4:0] s;\n  output p;\n"
                    "  assign s = a + b;\n  assign p = a[0] & b[3];\nendmodule\n")
    iface = {"module_name": "add4p", "inputs": [("a", 4), ("b", 4)], "outputs": [("s", 5), ("p", 1)],
             "relaxed_output": "s"}
    cands = generate_compressor_candidates(cone, iface, tmp_path)
    assert cands
    for c in cands:
        txt = Path(c["path"]).read_text().replace("module add4p(", "module cand(", 1)
        (tmp_path / "cand.v").write_text(txt)
        (tmp_path / "tb.v").write_text(
            "module tb; reg [3:0] a, b; wire [4:0] s0, s1; wire p0, p1; integer i, bad;\n"
            "  add4p ex(a, b, s0, p0); cand ap(a, b, s1, p1);\n"
            "  initial begin bad = 0; for (i = 0; i < 256; i = i + 1) begin {a, b} = i; #1 if (p0 !== p1) bad = bad + 1;"
            " end $display(\"BAD %0d\", bad); $finish; end\nendmodule\n")
        subprocess.run(["iverilog", "-g2012", "-s", "tb", "-o", str(tmp_path / "t.vvp"), str(tmp_path / "tb.v"),
                        str(cone), str(tmp_path / "cand.v"), str(xilinx_cells_sim())], check=True, capture_output=True)
        out = subprocess.run(["vvp", "-n", str(tmp_path / "t.vvp")], check=True, capture_output=True, text=True).stdout
        assert "BAD 0" in out, (c["name"], out)
