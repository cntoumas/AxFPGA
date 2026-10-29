"""Unit tests for fracturable Single-LUT6 compressors in AxFPGA."""

from pathlib import Path
import pytest
from axfpga.compressors import (
    generate_compressor_candidates,
    get_compressor_definitions,
    LUT6_2_COMP42_EXACT_INIT,
    LUT6_2_COMP42_APPROX_INIT,
    LUT6_2_COMP52_APPROX_INIT,
    LUT6_2_DUAL_ADDER_2B_INIT,
    COMPRESSOR_VERILOG_PRIMITIVES,
)


def test_compressor_verilog_definitions():
    """Verify that all LUT6_2 compressor templates contain valid INIT strings."""
    assert LUT6_2_COMP42_EXACT_INIT == "64'hfffefee896696996"
    assert LUT6_2_COMP42_APPROX_INIT == "64'hf888f8886ff66ff6"
    assert LUT6_2_COMP52_APPROX_INIT == "64'hfffefee896696996"
    assert LUT6_2_DUAL_ADDER_2B_INIT == "64'he81717e896969696"

    all_defs = get_compressor_definitions()
    assert "lut6_compressor_4_2" in all_defs
    assert "lut6_approx_compressor_4_2" in all_defs
    assert "lut6_approx_compressor_5_3" in all_defs
    assert "lut6_dual_adder_2b" in all_defs
    assert "LUT6_2" in COMPRESSOR_VERILOG_PRIMITIVES


def test_generate_compressor_candidates(tmp_path: Path):
    """Test generating candidates for an adder-like cone interface."""
    cone_v = tmp_path / "dummy_cone.v"
    cone_v.write_text("""
module test_adder (
    input [15:0] a,
    input [15:0] b,
    output [15:0] sum
);
    assign sum = a + b;
endmodule
""")
    iface = {
        "module_name": "test_adder",
        "inputs": [("a", 16), ("b", 16)],
        "outputs": [("sum", 16)],
        "input_names": ["a", "b"],
        "output_names": ["sum"],
    }
    cands = generate_compressor_candidates(cone_v, iface, tmp_path)
    assert len(cands) >= 4
    cand_names = [c["name"] for c in cands]
    assert any("comp42" in name for name in cand_names)
    assert any("comp53" in name for name in cand_names)
    for c in cands:
        assert Path(c["path"]).exists()
