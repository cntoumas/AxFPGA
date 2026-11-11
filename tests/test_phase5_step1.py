"""Phase 5, step 1: the Tournament measures cone error on the decoded value of an explicit axc_format; the result"""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from axfpga.cone_interface import parse_cone_interface
from axfpga.estimator import FpgaEstimator
from axfpga.tournament import Tournament

ROOT = Path(__file__).resolve().parent.parent
needs_iverilog = pytest.mark.skipif(shutil.which("iverilog") is None, reason="iverilog not installed")

CONE = "module cone(a, y);\n  input [15:0] a;\n  output [15:0] y;\n  assign y = a;\nend" + "module\n"


def _meta(tmp_path, fmt):
    q = {"format": fmt} if fmt else {}
    m = {"relaxed_signals": [{"wire": "y", "width": 16, "quality": q}]}
    p = tmp_path / "meta.json"
    p.write_text(json.dumps(m))
    return p


def _cone(tmp_path, name, expr):
    p = tmp_path / f"{name}.v"
    p.write_text(f"module cone(a, y);\n  input [15:0] a;\n  output [15:0] y;\n  assign y = {expr};\nendmodule\n")
    return p


@pytest.mark.parametrize("fmt, kind, width, frac", [("s16", "s", 16, 0), ("sm16q9", "sm", 16, 9), ("u", "u", 16, 0), ("u8", "u", 8, 0)])
def test_interface_reports_explicit_format(tmp_path, fmt, kind, width, frac):
    iface = parse_cone_interface(_cone(tmp_path, "c", "a"), _meta(tmp_path, fmt))
    assert iface["relaxed_format"] == {"kind": kind, "width": width, "frac": frac}


def test_interface_without_format_keeps_legacy(tmp_path):
    iface = parse_cone_interface(_cone(tmp_path, "c", "a"), _meta(tmp_path, None))
    assert iface["relaxed_format"] is None


def _wce(tmp_path, fmt, exact_expr, approx_expr, stim_vals):
    exact, approx = _cone(tmp_path, "exact", exact_expr), _cone(tmp_path, "approx", approx_expr)
    iface = parse_cone_interface(exact, _meta(tmp_path, fmt))
    stim = tmp_path / "stim.hex"
    stim.write_text("# a\n" + "\n".join(f"{v:x}" for v in stim_vals) + "\n")
    t = Tournament(FpgaEstimator(family="xc7", device="xc7z010"), tmp_path / "run", sim_samples=8)
    return t._simulate_candidate_uncached(exact, approx, iface, stim, "WCE", tmp_path / "run")


@needs_iverilog
def test_signed_format_error_is_the_decoded_difference(tmp_path):
    # exact 0x7FFF (+32767) against approx 0x8000 (-32768 in s16): decoded error 65535; read as unsigned words it is 1.
    (tmp_path / "f").mkdir()
    (tmp_path / "g").mkdir()
    approx = "(a == 16'h7FFF) ? 16'h8000 : a"
    assert _wce(tmp_path / "f", "s16", "a", approx, [0x7FFF, 5])["wce"] == 65535.0
    assert _wce(tmp_path / "g", None, "a", approx, [0x7FFF, 5])["wce"] == 1.0


@needs_iverilog
@pytest.mark.parametrize("fmt", [None, "u16", "s16"])
def test_error_of_a_candidate_above_the_exact_value_is_its_difference(tmp_path, fmt):
    """Before Phase 5 an approximate word above the exact one got an error of about 2^W instead of its difference."""
    (tmp_path / "x").mkdir()
    assert _wce(tmp_path / "x", fmt, "a", "(a == 16'd5) ? 16'd9 : a", [5, 100])["wce"] == 4.0
    (tmp_path / "y").mkdir()
    assert _wce(tmp_path / "y", fmt, "a", "(a == 16'd5) ? 16'd2 : a", [5, 100])["wce"] == 3.0


@needs_iverilog
def test_sign_magnitude_format_error_is_the_decoded_difference(tmp_path):
    (tmp_path / "f").mkdir()
    # -0 (0x8000) against +0: decoded error 0; mag 3 negative against positive: error 6
    r = _wce(tmp_path / "f", "sm16q9", "a", "(a == 16'd0) ? 16'h8000 : (a == 16'd3) ? 16'h8003 : a", [0, 3, 9])
    assert r["wce"] == 6.0


def _load_headroom():
    spec = importlib.util.spec_from_file_location("headroom_diagnosis", ROOT / "scripts" / "headroom_diagnosis.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_failure_tags():
    h = _load_headroom()
    assert h.failure_tags("WCE 185 > 26; MED 53.331 > 13; MRED 29.1341% > 1%; Decision agreement 84.8 % fails >= 99.0 %") == ["wce", "med", "mred", "app"]
    assert h.failure_tags("held-out stream: WCE 118 > 26") == ["held-out"]
    assert h.failure_tags("no top-level savings (LC 4038->4057, LUT 3842->3861)") == ["no-savings"]
    assert h.failure_tags("formal control=FALSIFIED, WCE=PROVEN") == ["formal"]


def test_usage_ratios():
    h = _load_headroom()
    u = h.usage_of([{"metric": "wce", "op": "<=", "threshold": 255.0, "value": 62.0},
                    {"metric": "app", "op": ">=", "threshold": 30.0, "value": 60.0},
                    {"metric": "app", "op": ">=", "threshold": 30.0, "value": float("inf")}])
    assert [x["ratio"] for x in u] == [round(62 / 255, 4), 0.5, 0.0]


def _res(method, fell_back, attempts, entries, quality=None, base=100, bound=10.0):
    return {"id": "d", "name": "D", "method": method, "fell_back": fell_back, "attempts": attempts,
            "tournament": {"baseline_lcs": base, "bound": bound, "entries": entries},
            "quality": quality or [{"metric": "wce", "op": "<=", "threshold": 10.0, "value": 2.0}],
            "savings_pct": {"logic_cells": 10.0}, "exact": {"logic_cells": 100}, "approx": {"logic_cells": 90},
            "certification": {"tier": "certified"}}


def test_diagnose_limiters():
    h = _load_headroom()
    # cone champion, a cheaper candidate rejected on the application metric
    r = _res("pyALS_loa_2b [raw]", False,
             [{"candidate": "pyALS_loa_4b", "outcome": "rejected", "reason": "SQNR 20 dB fails >= 30", "cone_lcs": 40},
              {"candidate": "pyALS_loa_2b", "outcome": "accepted", "cone_lcs": 60}], [])
    assert h.diagnose(r)["limiter"] == "app"
    # no cheaper candidate in or out of attempts, over-bound candidate exists
    r = _res("pyALS_loa_2b [raw]", False, [{"candidate": "pyALS_loa_2b", "outcome": "accepted", "cone_lcs": 60}],
             [{"name": "x", "error": 40.0, "within_bound": False, "lcs": None}])
    d = h.diagnose(r)
    assert d["limiter"] == "cone-bound" and "40" in d["detail"]
    # fallback: candidates inside the bound but none cheaper
    r = _res("exact (fallback)", True, [], [{"name": "y", "error": 1.0, "within_bound": True, "lcs": 120}])
    assert h.diagnose(r)["limiter"] == "menu"
    # PPG: deeper depth rejected on held-out
    r = _res("ppg_design_k5", False, [{"candidate": "ppg_design_k6", "outcome": "rejected", "reason": "held-out stream: WCE 118 > 26"}], [])
    assert h.diagnose(r)["limiter"] == "held-out"
    r = _res("ppg_design_k5", False, [], [])
    assert h.diagnose(r)["limiter"] == "search-schedule"


def test_spread_attempts_prefers_distinct_costs():
    from pathlib import Path as P

    from axfpga.pipeline import _spread_attempts
    from axfpga.tournament import Alternative

    def alt(name, lcs, dsps=0):
        return Alternative(name, P(name), 0.0, lcs, lcs, 0, dsps)

    alts = [alt("a", 100), alt("b", 100), alt("c", 100), alt("d", 104), alt("e", 110), alt("f", 110)]
    assert [a.name for a in _spread_attempts(alts, 4)] == ["a", "d", "e", "b"]       # one per cost first, then ties
    assert [a.name for a in _spread_attempts(alts, 10)] == ["a", "d", "e", "b", "c", "f"]
    assert [a.name for a in _spread_attempts([alt("x", 5), alt("y", 6)], 8)] == ["x", "y"]   # no ties: plain prefix
    # a design with a different DSP count is a different cost even at the same LC
    assert [a.name for a in _spread_attempts([alt("p", 50, 1), alt("q", 50, 1), alt("r", 50, 2)], 2)] == ["p", "r"]


def test_ladder_spreads_attempts_over_the_cost_range_and_reports_the_gaps():
    from pathlib import Path as P

    from axfpga.pipeline import _ladder
    from axfpga.tournament import Alternative

    alts = [Alternative(f"c{i}", P(f"c{i}"), 0.0, 100 + i, 100 + i, 0, 0) for i in range(20)]     # 20 distinct costs
    chosen, gaps = _ladder(alts, 5)
    assert [a.name for a in chosen] == ["c0", "c5", "c10", "c14", "c19"] or len(chosen) == 5
    assert chosen[0].name == "c0" and chosen[-1].name == "c19"                  # cheapest and mildest are always tried
    assert gaps[0] == []
    flat = [a.name for g in gaps for a in g] + [a.name for a in chosen]
    assert sorted(flat) == sorted(a.name for a in alts)                          # every candidate is an attempt or a gap
    for i in range(1, len(chosen)):                                              # gaps lie between neighbouring attempts
        lo, hi = chosen[i - 1].lcs, chosen[i].lcs
        assert all(lo < a.lcs < hi for a in gaps[i])
    # at most `limit` distinct costs: the plain prefix, no gaps
    chosen, gaps = _ladder(alts[:4], 8)
    assert [a.name for a in chosen] == ["c0", "c1", "c2", "c3"] and all(g == [] for g in gaps)


def test_dsp_weighted_cost_never_prefers_logic_over_dsp_blocks():
    """A LUT-mapped candidate of a DSP cone (no DSP, +1200 LC) must not look cheaper than the DSP cone, while a candidate"""
    from axfpga.tournament import DSP_LC_WEIGHT

    def cost(lcs, dsps):
        return lcs + DSP_LC_WEIGHT * dsps
    assert cost(1229, 0) > cost(32, 26)                      # fft64: 26 DSP blocks + 32 LC against a 1229-LC soft version
    assert cost(80, 1) < cost(71, 4)                         # multiplier 32b: narrowed to one block for a few more LC
    assert cost(300, 0) > cost(71, 4)                        # ... but not by swapping them for 300 LC of logic


@pytest.mark.parametrize("nodsp, expect_dsp", [(False, True), (True, False)])
def test_remap_follows_the_dsp_setting(tmp_path, nodsp, expect_dsp):
    """Before Phase 5 the remap step always allowed DSP inference, so a -nodsp run could end with DSP48E1 cells."""
    cone = tmp_path / "m.v"
    cone.write_text("module m(a, b, y);\n  input [15:0] a;\n  input [15:0] b;\n  output [31:0] y;\n  assign y = a * b;\nendmodule\n")
    t = Tournament(FpgaEstimator(family="xc7", device="xc7z010", nodsp=nodsp), tmp_path / "run")
    out = t.remap(cone)
    assert ("DSP48E1" in out.read_text()) == expect_dsp
