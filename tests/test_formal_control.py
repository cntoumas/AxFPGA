"""Formal control-equivalence proof (FormalControlVerifier) on small sequential designs.

Each case is an exact/approximate pair of the same top module with one precise output
`ctrl` and one relaxed output `data`; only `ctrl` is compared.
"""

import json
from pathlib import Path

import pytest

from axfpga.verifier import FormalControlVerifier

TOP = "dut"


def _module(ctrl_logic: str, extra_regs: str = "", data_expr: str = "x") -> str:
    return f"""
module {TOP}(input clk, input rst, input [7:0] x, output ctrl, output [7:0] data);
  reg [7:0] r;
  {extra_regs}
  always @(posedge clk) begin
    if (rst) r <= 8'd0; else r <= x;
  end
  {ctrl_logic}
  assign data = {data_expr};
endmodule
"""


def _verify(tmp_path: Path, exact: str, approx: str, depth: int = 5, timeout: int = 60) -> dict:
    e, a, m = tmp_path / "exact.v", tmp_path / "approx.v", tmp_path / "meta.json"
    e.write_text(exact)
    a.write_text(approx)
    m.write_text(json.dumps({"top_module": TOP, "relaxed_signals": [{"wire": "data", "bound": 4.0}]}))
    return FormalControlVerifier(e, a, meta_json=m, induction_depth=depth, timeout=timeout).verify()


EXACT = _module("assign ctrl = r[0];")


def test_equivalent_control_is_proven(tmp_path):
    approx = _module("assign ctrl = r[0];", data_expr="{x[7:2], 2'b00}")   # only the relaxed output differs
    res = _verify(tmp_path, EXACT, approx)
    assert res["status"] == "PROVEN"


def test_mismatch_in_first_cycles_is_falsified(tmp_path):
    # `started` powers up 0 and is set by the first clock edge, whatever rst does: ctrl differs
    # only in the very first time step and is unreachable afterwards. `-seq N -tempinduct`
    # alone skips steps 1..N and would call this PROVEN.
    approx = _module(
        "assign ctrl = r[0] | ~started;",
        extra_regs="reg started; always @(posedge clk) started <= 1'b1;",
    )
    res = _verify(tmp_path, EXACT, approx)
    assert res["status"] == "FALSIFIED", res


def test_permanent_mismatch_is_falsified(tmp_path):
    approx = _module("assign ctrl = r[1];")
    res = _verify(tmp_path, EXACT, approx)
    assert res["status"] == "FALSIFIED"


def test_true_but_not_inductive_property_is_bounded(tmp_path):
    # A saturating counter never reaches 65535 from reset, but k-induction cannot show it
    # (unreachable states 1001..65534 count up to 65535): only the bounded check succeeds.
    regs = "reg [15:0] cnt; always @(posedge clk) if (rst) cnt <= 0; else if (cnt != 16'd1000) cnt <= cnt + 1'b1;"
    exact = _module("assign ctrl = 1'b0;", extra_regs=regs)
    approx = _module("assign ctrl = (cnt == 16'hffff);", extra_regs=regs)
    res = _verify(tmp_path, exact, approx, depth=6, timeout=10)
    assert res["status"] == "BOUNDED", res
    assert res["cycles_checked"] == 6


def test_combinational_mismatch_is_falsified(tmp_path):
    comb = "module {t}(input [3:0] a, output ctrl, output [3:0] data); assign ctrl = {c}; assign data = a; endmodule"
    res = _verify(tmp_path, comb.format(t=TOP, c="a[0]"), comb.format(t=TOP, c="a[1]"))
    assert res["status"] == "FALSIFIED"
