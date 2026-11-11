"""Formal WCE proof (FormalWceVerifier) and relaxed-wire to Cone-port matching."""

import json
from pathlib import Path

import pytest

from axfpga.verifier import FormalWceVerifier, match_cone_output

CONE = """
module cone(a, b, \\U0.Po , \\U0.S );
  input [3:0] a;
  input [3:0] b;
  output \\U0.Po ;
  output [4:0] \\U0.S ;
  assign \\U0.S = {s};
  assign \\U0.Po = a[0] & b[0];
endmodule
"""
EXACT = CONE.format(s="a + b")
APPROX = CONE.format(s="{{1'b0, a[3:1]} + {1'b0, b[3:1]}, 1'b0}")    # drops bit 0: |error| <= 2


def _verify(tmp_path: Path, signals: list, approx: str = APPROX) -> dict:
    e, a, m = tmp_path / "exact.v", tmp_path / "approx.v", tmp_path / "meta.json"
    e.write_text(EXACT)
    a.write_text(approx)
    m.write_text(json.dumps({"top_module": "cone", "relaxed_signals": signals}))
    return FormalWceVerifier(e, a, meta_json=m, timeout=60).verify()


def test_bound_holds_on_the_relaxed_port(tmp_path):
    assert _verify(tmp_path, [{"wire": "S", "bound": 2.0}])["status"] == "PROVEN"


def test_bound_violation_is_falsified(tmp_path):
    assert _verify(tmp_path, [{"wire": "S", "bound": 1.0}])["status"] == "FALSIFIED"


def test_unknown_relaxed_wire_is_an_error_not_a_guess(tmp_path):
    # Before: silently fell back to the first output (the 1-bit carry port) and "proved" it.
    with pytest.raises(ValueError, match="matches 0 Cone outputs"):
        _verify(tmp_path, [{"wire": "Z", "bound": 2.0}])


def test_missing_bound_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no axc_bound"):
        _verify(tmp_path, [{"wire": "S"}])


def test_match_cone_output():
    assert match_cone_output("S", ["U0.Po", "U0.S"]) == "U0.S"
    assert match_cone_output("out", ["out", "M2.out"]) == "out"          # exact name wins
    assert match_cone_output("dataout", ["dataout"]) == "dataout"
    with pytest.raises(ValueError):
        match_cone_output("S", ["A.S", "B.S"])                           # ambiguous
    with pytest.raises(ValueError):
        match_cone_output("S", ["U0.SUM"])                              # no prefix-less match


def _verify_fmt(tmp_path: Path, exact: str, approx: str, sig: dict) -> dict:
    e, a, m = tmp_path / "e.v", tmp_path / "a.v", tmp_path / "m.json"
    e.write_text(exact)
    a.write_text(approx)
    m.write_text(json.dumps({"top_module": "c", "relaxed_signals": [sig]}))
    return FormalWceVerifier(e, a, meta_json=m, timeout=60).verify()


C8 = "module c(x, y);\n  input [7:0] x;\n  output [7:0] y;\n  assign y = {e};\nendmodule\n"


def test_signed_format_reads_the_words_as_twos_complement(tmp_path):
    # +127 against -128 (0x7F vs 0x80): decoded error 255; read as unsigned words the difference is 1.
    exact, approx = C8.format(e="x"), C8.format(e="(x == 8'h7F) ? 8'h80 : x")
    assert _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 100.0, "quality": {"format": "s8"}})["status"] == "FALSIFIED"
    assert _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 255.0, "quality": {"format": "s8"}})["status"] == "PROVEN"
    assert _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 1.0, "quality": {"format": "u8"}})["status"] == "PROVEN"


def test_sign_magnitude_format_reads_sign_and_magnitude(tmp_path):
    # -0 (0x80) against +0: decoded error 0. 0x83 (-3) against 0x03 (+3): 6.
    exact = C8.format(e="x")
    approx = C8.format(e="(x == 8'h80) ? 8'h00 : (x == 8'h83) ? 8'h03 : x")
    assert _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 6.0, "quality": {"format": "sm8"}})["status"] == "PROVEN"
    assert _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 5.0, "quality": {"format": "sm8"}})["status"] == "FALSIFIED"


def test_cone_output_hint_selects_the_port(tmp_path):
    src = "module c(x, \\u.out );\n  input [7:0] x;\n  output [7:0] \\u.out ;\n  assign \\u.out = {e};\nendmodule\n"
    exact, approx = src.format(e="x"), src.format(e="x & 8'hFE")
    assert _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 1.0, "cone_output": "u.out"})["status"] == "PROVEN"
    with pytest.raises(ValueError, match="matches 0 Cone outputs"):
        _verify_fmt(tmp_path, exact, approx, {"wire": "y", "bound": 1.0})


def test_signal_of_another_cone_is_skipped_and_an_empty_check_is_an_error(tmp_path):
    src = "module c(x, \\u.out );\n  input [7:0] x;\n  output [7:0] \\u.out ;\n  assign \\u.out = {e};\nendmodule\n"
    exact, approx = src.format(e="x"), src.format(e="x & 8'hFE")
    sigs = [{"wire": "p", "bound": 1.0, "cone_output": "other.out"}, {"wire": "y", "bound": 1.0, "cone_output": "u.out"}]
    e, a, m = tmp_path / "e.v", tmp_path / "a.v", tmp_path / "m.json"
    e.write_text(exact)
    a.write_text(approx)
    m.write_text(json.dumps({"top_module": "c", "relaxed_signals": sigs}))
    assert FormalWceVerifier(e, a, meta_json=m, timeout=60).verify()["status"] == "PROVEN"
    m.write_text(json.dumps({"top_module": "c", "relaxed_signals": sigs[:1]}))
    with pytest.raises(ValueError, match="no relaxed signal is carried"):
        FormalWceVerifier(e, a, meta_json=m, timeout=60).verify()
