"""Cone Interface relaxed-output and signedness rules."""

import json
from pathlib import Path

from axfpga.cone_interface import parse_cone_interface

CONE = """module cone(a, b, narrow, wide);
  input [7:0] a;
  input [7:0] b;
  output [3:0] narrow;
  output [15:0] wide;
  assign narrow = a[3:0] + b[3:0];
  assign wide = a * b;
endmodule
"""


def test_widest_multibit_output_is_relaxed_without_meta(tmp_path: Path):
    p = tmp_path / "cone.v"
    p.write_text(CONE)
    iface = parse_cone_interface(p)
    assert iface["relaxed_output"] == "wide"
    assert iface["outputs"]["wide"]["signed"] is False


def test_meta_relaxed_wire_wins_when_it_is_a_cone_output(tmp_path: Path):
    p = tmp_path / "cone.v"
    p.write_text(CONE)
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"relaxed_signals": [{"wire": "missing"}, {"wire": "narrow"}]}))
    assert parse_cone_interface(p, meta)["relaxed_output"] == "narrow"


def test_meta_wire_not_in_cone_falls_back_to_widest(tmp_path: Path):
    p = tmp_path / "cone.v"
    p.write_text(CONE)
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"relaxed_signals": [{"wire": "acc_out"}]}))
    assert parse_cone_interface(p, meta)["relaxed_output"] == "wide"


def test_signed_declaration_marks_relaxed_output_signed(tmp_path: Path):
    p = tmp_path / "cone.v"
    p.write_text(CONE.replace("output [15:0] wide;", "output signed [15:0] wide;"))
    iface = parse_cone_interface(p)
    assert iface["outputs"]["wide"]["signed"] is True
