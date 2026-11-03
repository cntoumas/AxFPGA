"""Formal WCE proof (FormalWceVerifier) and relaxed-wire to Cone-port matching."""

import json
from pathlib import Path

import pytest

from axfpga.verifier import FormalWceVerifier, match_cone_output

# Extracted Cones keep hierarchical port names: the relaxed wire `S` is the port `\U0.S`,
# next to an auxiliary carry port `\U0.Po`.
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
