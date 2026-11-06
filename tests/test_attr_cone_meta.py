"""ConeExtractor: axc_* attributes end up in the cone metadata."""

import json
from pathlib import Path

import pytest

from axfpga.cone_extractor import ConeExtractor

TOY = """module toy(
  (* axc_clock *) input clk,
  (* axc_reset = "low" *) input nres,
  input [7:0] a,
  input [7:0] b,
  (* axc_relax, axc_format = "u16", axc_quality = "wce<=64; med<=20; mred<=2%",
     axc_seed = 5, axc_max_sites = 4, axc_ops = "trunc,loa" *) output reg [15:0] y
);
  always @(posedge clk) if (!nres) y <= 0; else y <= a * b;
endmodule
"""


def run(tmp_path: Path, src: str):
    v = tmp_path / "toy.v"
    v.write_text(src)
    out = tmp_path / "meta.json"
    ConeExtractor(v).extract_safe_cone(out_meta_path=out,
                                       out_submod_path=tmp_path / "c.v",
                                       out_top_path=tmp_path / "t.v")
    return json.loads(out.read_text())


def test_attributes_in_meta(tmp_path):
    meta = run(tmp_path, TOY)
    assert meta["clock"] == "clk"
    assert meta["reset"] == "nres" and meta["reset_active_low"] is True
    sig = meta["relaxed_signals"][0]
    assert sig["bound"] == 64
    q = sig["quality"]
    assert q["format"] == "u16" and q["seed"] == 5
    assert q["quality"] == "wce<=64; med<=20; mred<=2%"
    assert [c["metric"] for c in q["constraints"]] == ["wce", "med", "mred"]
    assert sig["reserved"] == {"max_sites": 4, "ops": ["trunc", "loa"]}


def test_legacy_attrs_unchanged_no_quality_key(tmp_path):
    meta = run(tmp_path, """module t(input [7:0] a, input [7:0] b,
      (* axc_relax, axc_bound = 12 *) output [15:0] y);
      assign y = a * b; endmodule""")
    sig = meta["relaxed_signals"][0]
    assert sig["bound"] == 12 and "quality" not in sig and "reserved" not in sig
    assert "clock" not in meta and "reset" not in meta


def test_bare_reset_marker_is_active_high(tmp_path):
    meta = run(tmp_path, TOY.replace('(* axc_reset = "low" *)', "(* axc_reset *)"))
    assert meta["reset_active_low"] is False


def test_bad_quality_raises_naming_wire(tmp_path):
    with pytest.raises(ValueError, match="axc_quality of 'y'.*unknown metric 'foo'"):
        run(tmp_path, TOY.replace("mred<=2%", "foo<=2"))


def test_missing_bound_raises(tmp_path):
    with pytest.raises(ValueError, match="needs a wce<= constraint"):
        run(tmp_path, TOY.replace("wce<=64; ", ""))


def test_two_clocks_rejected(tmp_path):
    with pytest.raises(ValueError, match="two inputs carry axc_clock"):
        run(tmp_path, TOY.replace("input [7:0] a,", "(* axc_clock *) input c2, input [7:0] a,"))


def test_clock_on_output_rejected(tmp_path):
    with pytest.raises(ValueError, match="only input ports"):
        run(tmp_path, TOY.replace("axc_seed = 5,", "axc_clock,"))


def test_cone_output_names_the_port_that_carries_the_relaxed_wire(tmp_path):
    meta = run(tmp_path, """module t(input clk, input [7:0] a, input [7:0] b,
      (* axc_relax, axc_bound = 12 *) output reg [8:0] y);
      always @(posedge clk) y <= a + b; endmodule""")
    sig = meta["relaxed_signals"][0]
    ports = [p["name"] for p in meta["cones"][0]["submodule_outputs"]]
    assert sig["cone_output"] in ports
