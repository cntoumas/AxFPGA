"""Unit tests for FpgaEstimator."""

from pathlib import Path
import pytest
from axfpga.fpga_estimator import FpgaEstimator


@pytest.fixture
def sample_adder_vlog(tmp_path: Path) -> Path:
    f = tmp_path / "simple_add.v"
    f.write_text("""
    module simple_add (
        input  wire [15:0] a,
        input  wire [15:0] b,
        output wire [16:0] sum
    );
        assign sum = a + b;
    endmodule
    """)
    return f


@pytest.fixture
def sample_approx_adder_vlog(tmp_path: Path) -> Path:
    f = tmp_path / "approx_add.v"
    # Lower 4 bits zeroed out (truncated)
    f.write_text("""
    module simple_add (
        input  wire [15:0] a,
        input  wire [15:0] b,
        output wire [16:0] sum
    );
        wire [12:0] high_sum = a[15:4] + b[15:4];
        assign sum = {high_sum, 4'b0000};
    endmodule
    """)
    return f


def test_fpga_estimator_evaluation(sample_adder_vlog: Path, tmp_path: Path):
    out_v = tmp_path / "mapped_fpga.v"
    estimator = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=True)
    res = estimator.evaluate_netlist(sample_adder_vlog, top_module="simple_add", out_mapped_vlog=out_v)

    assert res["top_module"] == "simple_add"
    assert res["family"] == "xc7"
    assert res["device"] == "xc7a35t"
    assert res["carry4"] > 0  # 16-bit add should infer CARRY4
    assert res["total_luts"] >= 0
    assert res["estimated_lcs"] > 0
    assert res["lut_utilization_pct"] < 1.0  # tiny design on Artix-7
    assert out_v.exists()


def test_fpga_estimator_comparison(sample_adder_vlog: Path, sample_approx_adder_vlog: Path):
    estimator = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=True)
    cmp_res = estimator.compare_exact_vs_approx(
        exact_netlist=sample_adder_vlog,
        approx_netlist=sample_approx_adder_vlog,
        top_module="simple_add",
    )

    assert cmp_res["top_module"] == "simple_add"
    assert "exact" in cmp_res
    assert "approx" in cmp_res
    # Truncated adder should use fewer or equal carry/LCs
    assert cmp_res["lcs_saved"] >= 0
    assert cmp_res["lc_reduction_pct"] >= 0.0
