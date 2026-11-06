"""Relaxation behind internal registers: ConeExtractor(through_registers=True)."""

import json
from pathlib import Path

from axfpga.cone_extractor import ConeExtractor

PIPE = """module p(
  input clk,
  input rst,
  (* axc_precise *) input en,
  input [7:0] a,
  input [7:0] b,
  (* axc_precise *) output reg vld,
  (* axc_relax, axc_bound = 64, axc_format = "u18" *) output reg [17:0] y
);
  reg [8:0]  s1;
  reg [11:0] c1;
  reg [3:0]  cnt;
  reg        v1;
  always @(posedge clk) begin
    if (rst) begin s1 <= 0; c1 <= 0; y <= 0; cnt <= 0; v1 <= 0; vld <= 0; end
    else begin
      s1  <= a + b;                    // stage 1: behind two registers
      c1  <= s1 * 3;                   // stage 2: behind one register
      y   <= c1 + {3'd0, s1};          // last stage: the normal cone
      cnt <= cnt + {3'd0, en};         // precise state
      v1  <= en & (cnt != 4'd0);
      vld <= v1;
    end
  end
endmodule
"""


def extract(tmp_path: Path, src: str, through: bool):
    v = tmp_path / "p.v"
    v.write_text(src)
    meta = ConeExtractor(v, through_registers=through).extract_safe_cone(
        out_submod_path=tmp_path / "c.v", out_top_path=tmp_path / "t.v", out_meta_path=tmp_path / "m.json")
    return meta


def kinds(sites):
    return sorted(s.split("@")[0] for s in sites)


def test_off_by_default_and_meta_unchanged(tmp_path):
    meta = extract(tmp_path, PIPE, False)
    assert "through_registers" not in meta


def test_arithmetic_behind_registers_is_found(tmp_path):
    meta = extract(tmp_path, PIPE, True)
    t = meta["through_registers"]
    assert kinds(t["cone_sites"]) == ["add"]                                   # the normal cone: y = c1 + s1
    assert kinds(t["sites_behind_registers"]) == ["add", "mul"]                # a + b and s1 * 3
    assert t["num_registers_relaxed"] >= 2


def test_precise_state_is_never_relaxed(tmp_path):
    meta = extract(tmp_path, PIPE, True)
    t = meta["through_registers"]
    lines = {s.split(":")[1] for s in t["sites"]}                              # source positions, e.g. "23.19-23.31"
    precise_line = next(i + 1 for i, l in enumerate(PIPE.splitlines()) if "cnt <= cnt +" in l)
    assert not any(l.startswith(f"{precise_line}.") for l in lines)            # the counter's adder is not a site


def test_register_feeding_precise_logic_stays_precise(tmp_path):
    src = PIPE.replace("v1  <= en & (cnt != 4'd0);", "v1  <= en & (s1 != 9'd0);")      # now s1 feeds the precise valid chain
    meta = extract(tmp_path, src, True)
    t = meta["through_registers"]
    assert "add" not in kinds(t["sites_behind_registers"])                      # a + b computes s1: precise
    assert kinds(t["sites_behind_registers"]) == ["mul"] or kinds(t["sites_behind_registers"]) == []


def test_no_relaxation_through_a_register_that_drives_only_precise_logic(tmp_path):
    src = PIPE.replace("y   <= c1 + {3'd0, s1};", "y   <= {6'd0, c1};")
    meta = extract(tmp_path, src, True)
    t = meta["through_registers"]
    assert kinds(t["sites_behind_registers"]).count("mul") == 1                 # c1 drives y: its mul is relaxed


def test_design_level_truncation_changes_only_the_relaxed_output(tmp_path):
    import random
    import shutil

    import pytest

    from axfpga.activity_power import run_vector_sim
    from axfpga.operators.design_level import run_design_level
    if shutil.which("iverilog") is None:
        pytest.skip("iverilog not installed")
    v = tmp_path / "p.v"
    v.write_text(PIPE)
    meta = ConeExtractor(v, through_registers=True).extract_safe_cone(
        out_submod_path=tmp_path / "c.v", out_top_path=tmp_path / "t.v", out_meta_path=tmp_path / "m.json")
    sites = meta["through_registers"]["sites"]
    res = run_design_level([str(v)], "p", sites, {"g": {"k": 3, "comp": "mid"}}, str(tmp_path / "dl"))
    assert not res["errors"] and res["applied"]["g"] == 3 and not res["missing"]
    rnd = random.Random(1)
    vecs = [{"en": rnd.getrandbits(1), "a": rnd.getrandbits(8), "b": rnd.getrandbits(8)} for _ in range(300)]
    kw = dict(top="p", inputs=[("en", 1), ("a", 8), ("b", 8)], outputs=[("vld", 1), ("y", 18)], vectors=vecs, clk="clk", rst="rst")
    exact = run_vector_sim([str(v)], work_dir=tmp_path / "se", tag="e", **kw)["outputs"]
    approx = run_vector_sim([res["written"]["g"]], work_dir=tmp_path / "sa", tag="a", **kw)["outputs"]
    assert [r["vld"] for r in exact] == [r["vld"] for r in approx]               # precise output identical
    errs = [abs(e["y"] - a["y"]) for e, a in zip(exact, approx)]
    assert max(errs) > 0                                                         # the relaxed output is approximate
    assert max(errs) < 2000
