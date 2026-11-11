import pytest
from pathlib import Path
from axfpga.ppg_pruner import PpgPruner


def test_ppg_error_calculation():
    # k = 1: terms = 1 * 2^0 = 1
    assert PpgPruner.calculate_worst_case_error(1) == 1
    # k = 2: 1 + 2 * 2 = 5
    assert PpgPruner.calculate_worst_case_error(2) == 5
    # k = 4: 1 + 4 + 12 + 32 = 49
    assert PpgPruner.calculate_worst_case_error(4) == 49
    # k = 5: 49 + 5 * 16 = 129
    assert PpgPruner.calculate_worst_case_error(5) == 129


def test_compute_max_safe_depth():
    assert PpgPruner.compute_max_safe_depth(bound=49.0) == 4
    assert PpgPruner.compute_max_safe_depth(bound=255.0) == 5
    assert PpgPruner.compute_max_safe_depth(bound=4096.0) >= 6


def test_prune_ppg_verilog():
    sample_vlog = """
module UB1BPPG_0_0(O, IN1, IN2);
  output O;
  input IN1, IN2;
  assign O = IN1 & IN2;
endmodule

module UB1BPPG_1_0(O, IN1, IN2);
  output O;
  input IN1, IN2;
  assign O = IN1 & IN2;
endmodule

module UB1BPPG_3_3(O, IN1, IN2);
  output O;
  input IN1, IN2;
  assign O = IN1 & IN2;
endmodule
"""
    pruner = PpgPruner()
    mod_code, count = pruner.prune_code_ppg(sample_vlog, depth=2)
    assert count == 2
    assert "assign O = 1'b0;" in mod_code
    assert "UB1BPPG_3_3" in mod_code
