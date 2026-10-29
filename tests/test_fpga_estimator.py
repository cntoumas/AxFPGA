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
    estimator = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=False)
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
    estimator = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=False)
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


def test_lut6_compressor_mapping(tmp_path: Path):
    from axfpga.compressors import get_compressor_definitions
    comp_defs = get_compressor_definitions()
    vlog_content = f"""
{comp_defs}

module test_comp_top(
    input  wire a, b, c, d, cin,
    output wire sum42, carry42,
    output wire sum53, carry53
);
    lut6_compressor_4_2 comp42 (
        .x1(a), .x2(b), .x3(c), .x4(d), .cin(cin),
        .sum(sum42), .carry(carry42)
    );
    lut6_approx_compressor_5_3 comp53 (
        .x1(a), .x2(b), .x3(c), .x4(d), .x5(cin),
        .sum(sum53), .carry(carry53)
    );
endmodule
"""
    vlog_file = tmp_path / "test_comp_top.v"
    vlog_file.write_text(vlog_content, encoding="utf-8")

    estimator = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=False)
    res = estimator.evaluate_netlist(vlog_file, top_module="test_comp_top")
    assert res["top_module"] == "test_comp_top"
    assert "LUT6_2" in res["luts_by_type"]
    assert res["luts_by_type"]["LUT6_2"] == 2
    assert res["total_luts"] == 2
    assert res["carry4"] == 0  # Compressors require 0 CARRY4s


def test_fpga_dsp_inference(tmp_path: Path):
    mult_vlog = """
module mult16(input clk, input [15:0] a, b, output reg [31:0] p);
    always @(posedge clk) p <= a * b;
endmodule
"""
    mult_file = tmp_path / "mult16.v"
    mult_file.write_text(mult_vlog, encoding="utf-8")

    # With nodsp=False (default), Yosys should infer DSP48E1
    estimator_dsp = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=False)
    res_dsp = estimator_dsp.evaluate_netlist(mult_file, top_module="mult16")
    assert res_dsp["dsps"] == 1
    assert res_dsp["total_luts"] == 0

    # With nodsp=True, Yosys should infer soft logic LUTs and 0 DSPs
    estimator_nodsp = FpgaEstimator(family="xc7", device="xc7a35t", nodsp=True)
    res_nodsp = estimator_nodsp.evaluate_netlist(mult_file, top_module="mult16")
    assert res_nodsp["dsps"] == 0
    assert res_nodsp["total_luts"] > 0
