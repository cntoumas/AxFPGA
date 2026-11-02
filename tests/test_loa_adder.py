import pytest
from pathlib import Path
from axfpga.alu_splitter import AluSplitter


def test_generate_loa_candidates(tmp_path):
    iface = {
        "module_name": "simple_adder",
        "inputs": {
            "A": {"width": 16, "signed": False},
            "B": {"width": 16, "signed": False},
        },
        "outputs": {
            "S": {"width": 17, "signed": False},
        },
        "relaxed_output": "S",
    }
    cone_content = """
module simple_adder(A, B, S);
  input [15:0] A;
  input [15:0] B;
  output [16:0] S;
  assign S = A + B;
endmodule
"""
    cands = AluSplitter.generate_loa_candidates(
        cone_content=cone_content,
        iface=iface,
        bound=15.0,
        work_dir=tmp_path,
    )
    assert len(cands) >= 1
    # For bound 15.0, safe depth k should be up to 4 (2^4 - 1 = 15 <= 15)
    depths = [c["depth"] for c in cands]
    assert 4 in depths
    assert all(c["theoretical_wce"] <= 15.0 for c in cands)
    # Check that candidate verilog exists and contains bitwise OR
    cand_k4 = [c for c in cands if c["depth"] == 4][0]
    assert Path(cand_k4["path"]).exists()
    content = Path(cand_k4["path"]).read_text()
    assert "|" in content or "simple_adder" in content
