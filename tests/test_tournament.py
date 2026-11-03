"""Tournament champion-rule tests through its interface (real yosys + iverilog)."""

from pathlib import Path

import pytest

from axfpga.estimator import FpgaEstimator
from axfpga.pyals_bridge import PyAlsBridge
from axfpga.tournament import Champion, Tournament

REPO_ROOT = Path(__file__).resolve().parent.parent
MAC_CONE = REPO_ROOT / "benchmarks" / "mac" / "mac.v"
CATALOG_DB = REPO_ROOT / "pyALS-lut-catalog" / "lut_catalog.db"

EXACT = """module add8(a, b, s);
  input [7:0] a;
  input [7:0] b;
  output [8:0] s;
  assign s = a + b;
endmodule
"""


def _truncated(k: int) -> str:
    return f"""module add8(a, b, s);
  input [7:0] a;
  input [7:0] b;
  output [8:0] s;
  wire [{8 - k}:0] hi = a[7:{k}] + b[7:{k}];
  assign s = {{hi, {k}'b0}};
endmodule
"""


@pytest.fixture
def cone(tmp_path: Path) -> Path:
    p = tmp_path / "add8.v"
    p.write_text(EXACT)
    return p


def _fake_generator(*names: str):
    sources = {"exact_copy": EXACT, "trunc2": _truncated(2), "trunc6": _truncated(6)}

    def gen(iface, work_dir: Path, bound: float):
        cands = []
        for n in names:
            p = work_dir / f"cand_{n}.v"
            p.write_text(sources[n])
            cands.append({"name": n, "path": p})
        return cands

    return gen


def _run(tmp_path: Path, cone: Path, *names: str) -> Champion:
    t = Tournament(FpgaEstimator(family="xc7", device="xc7a35t"), tmp_path / "work", sim_samples=256)
    return t.run(cone, None, metric="WCE", bound=6.0, generators=[_fake_generator(*names)])


def test_cheapest_candidate_within_bound_wins(tmp_path: Path, cone: Path):
    champ = _run(tmp_path, cone, "exact_copy", "trunc2", "trunc6")

    assert champ.name == "trunc2"
    assert not champ.fell_back
    assert champ.error <= 6.0
    assert champ.lcs < champ.baseline_lcs
    assert champ.lc_savings_pct > 0.0
    assert champ.netlist_path.exists()

    board = {e.name: e for e in champ.leaderboard}
    assert set(board) == {"exact_copy", "trunc2", "trunc6"}
    assert board["trunc6"].passed_bound is False
    assert board["trunc6"].lcs is None
    assert board["exact_copy"].passed_bound is True

    # Alternatives: only qualifying, resource-saving candidates, champion first
    assert [a.name for a in champ.alternatives] == ["trunc2"]


def test_falls_back_when_no_candidate_both_meets_bound_and_saves(tmp_path: Path, cone: Path):
    champ = _run(tmp_path, cone, "exact_copy", "trunc6")

    assert champ.fell_back
    assert champ.name == "baseline_fallback"
    assert champ.error == 0.0
    assert champ.lcs == champ.baseline_lcs
    assert champ.lc_savings_pct == 0.0


def test_pyals_generator_on_mac(tmp_path: Path):
    work = tmp_path / "work"
    bridge = PyAlsBridge(cone_verilog_path=MAC_CONE, work_dir=work, catalog_db_path=CATALOG_DB)
    t = Tournament(FpgaEstimator(family="xc7", device="xc7a35t"), work, sim_samples=50)
    champ = t.run(MAC_CONE, None, metric="MED", bound=500.0, generators=[bridge])

    assert champ.netlist_path.exists()
    assert champ.baseline_lcs > 0
    assert len(champ.leaderboard) > 0


def _aliased_generator(*names: str):
    """Every candidate is the same 2-bit truncation under a different name (equal cost and error)."""

    def gen(iface, work_dir: Path, bound: float):
        cands = []
        for n in names:
            p = work_dir / f"cand_{n}.v"
            p.write_text(_truncated(2))
            cands.append({"name": n, "path": p})
        return cands

    return gen


@pytest.mark.parametrize("order", [("zeta", "alpha", "mid"), ("alpha", "mid", "zeta"), ("mid", "zeta", "alpha")])
def test_equal_cost_candidates_rank_by_name_not_generation_order(tmp_path: Path, cone: Path, order):
    t = Tournament(FpgaEstimator(family="xc7", device="xc7a35t"), tmp_path / "work", sim_samples=256)
    champ = t.run(cone, None, metric="WCE", bound=6.0, generators=[_aliased_generator(*order)])
    assert champ.name == "alpha"
    assert [a.name for a in champ.alternatives] == ["alpha", "mid", "zeta"]
