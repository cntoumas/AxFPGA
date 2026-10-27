"""Unit tests for Native FPGA Arithmetic Approximation (LOA and BAM)."""

from pathlib import Path
import pytest
from axfpga.fpga_backend import FpgaBackend
from axfpga.fpga_estimator import FpgaEstimator


def test_fpga_loa_candidate_generation(tmp_path: Path):
    """Verify LOA generation and error compliance on multi-bit adder."""
    cone_code = """
    module adder_cone (
        input  wire [15:0] a,
        input  wire [15:0] b,
        output wire [16:0] sum
    );
        assign sum = a + b;
    endmodule
    """
    cone_file = tmp_path / "adder_cone.v"
    cone_file.write_text(cone_code, encoding="utf-8")

    backend = FpgaBackend(
        cone_verilog_path=cone_file,
        work_dir=tmp_path / "loa_work",
        target_family="xc7",
        target_device="xc7a35t",
    )

    iface = backend._parse_cone_interface()
    assert iface["relaxed_output"] == "sum"

    cands = backend._generate_loa_candidates(iface, tmp_path / "loa_work")
    assert len(cands) > 0
    assert any(c["strategy"] == "loa" for c in cands)

    # Check generated Verilog contains OR and carry logic
    sample_cand = cands[3]  # k=4
    cand_text = sample_cand["path"].read_text(encoding="utf-8")
    assert "_loa_lower" in cand_text
    assert "_loa_carry" in cand_text
    assert "_loa_upper" in cand_text


def test_fpga_bam_candidate_generation_and_savings(tmp_path: Path):
    """Verify BAM candidate generation and Logic Cell reduction on multiplier."""
    cone_code = """
    module mac_cone (
        input  wire signed [7:0] a,
        input  wire signed [7:0] b,
        input  wire signed [15:0] c,
        output wire signed [16:0] y
    );
        assign y = (a * b) + c;
    endmodule
    """
    cone_file = tmp_path / "mac_cone.v"
    cone_file.write_text(cone_code, encoding="utf-8")

    backend = FpgaBackend(
        cone_verilog_path=cone_file,
        work_dir=tmp_path / "bam_work",
        target_family="xc7",
        target_device="xc7a35t",
    )

    iface = backend._parse_cone_interface()
    cands = backend._generate_bam_candidates(iface, tmp_path / "bam_work")
    assert len(cands) > 0
    assert any(c["strategy"] == "bam" for c in cands)

    # Run tournament optimization specifically requesting BAM
    result = backend.optimize(
        metric="MED",
        bound=64.0,
        method="bam",
        out_approx_path=tmp_path / "approx_mac.v",
    )

    assert result["status"] == "SUCCESS"
    assert "BAM" in result["method"]
    assert result["lc_savings_pct"] > 0
    assert result["optimized_lcs"] < result["baseline_lcs"]
    assert Path(result["output_file"]).exists()
