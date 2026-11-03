"""Unit tests for Plan 1.1: CARRY4 chain protection and 4-bit nibble-aligned ALU splitting."""

import tempfile
from pathlib import Path
import pytest
from pyosys import libyosys as ys

from axfpga.alu_splitter import AluSplitter
from axfpga.pyals_ext import prep_design
from pyalslib import YosysHelper


def test_identify_and_split_wide_alu():
    """Verify pre-YosysHelper arithmetic extraction identifies W >= 8 and partitions at 4-bit boundaries."""
    vlog = """
    module adder32 (
        input [31:0] a,
        input [31:0] b,
        output [31:0] sum
    );
        assign sum = a + b;
    endmodule
    """
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w") as f:
        f.write(vlog)
        f.flush()

        design = ys.Design()
        ys.run_pass(f"read_verilog -sv {f.name}; hierarchy -check -auto-top", design)

        splitter = AluSplitter(min_width=8, approx_nibble_bits=4)
        split_count = splitter.protect_and_split(design, top_module_name="adder32", as_blackbox=True)

        assert split_count == 1, "Should identify and split the 32-bit adder"
        top_mod = design.top_module()
        cell_types = [c.type.str() for c in top_mod.selected_cells()]

        # Should contain a 4-bit LSB $alu and a protected MSB module instance
        assert "$alu" in cell_types or r"\$alu" in cell_types
        assert any("exact_alu_msb_28b" in ct for ct in cell_types)

        # Run techmap and abc -lut 4: the exact_alu_msb module must NOT be flattened!
        ys.run_pass("techmap; opt; abc -lut 4; clean", design)
        cells_after = [c.type.str() for c in top_mod.selected_cells()]
        assert any("exact_alu_msb_28b" in ct for ct in cells_after), "MSB adder must remain protected after techmap!"


def test_carry_truncation_constant_ci():
    """Verify that constant CI (CI = 1'b0) can be applied to break carry propagation."""
    vlog = """
    module adder16 (
        input [15:0] a,
        input [15:0] b,
        output [15:0] sum
    );
        assign sum = a + b;
    endmodule
    """
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w") as f:
        f.write(vlog)
        f.flush()

        design = ys.Design()
        ys.run_pass(f"read_verilog -sv {f.name}; hierarchy -check -auto-top", design)

        splitter = AluSplitter(min_width=8, approx_nibble_bits=4, constant_ci=True)
        split_count = splitter.protect_and_split(design, top_module_name="adder16", as_blackbox=True)

        assert split_count == 1
        top_mod = design.top_module()
        inst_msb = [c for c in top_mod.selected_cells() if "exact_alu_msb" in c.type.str()][0]
        ci_sig = inst_msb.getPort(ys.IdString(r"\CI"))
        # Constant zero check
        assert ci_sig.is_fully_const()
        assert ci_sig.as_const().as_int() == 0


def test_yosys_helper_protect_carry_chains():
    """Verify prep_design (pyals_ext) protects carry chains."""
    vlog = """
    module add_sub (
        input [15:0] a,
        input [15:0] b,
        input sub,
        output [15:0] res
    );
        assign res = sub ? (a - b) : (a + b);
    endmodule
    """
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w") as f:
        f.write(vlog)
        f.flush()

        helper = YosysHelper()
        helper.read_sources(f.name, "add_sub")
        # Default prep_design protects carry chains
        prep_design(helper, cut_size=4, protect_carry_chains=True, approx_nibble_bits=4)

        mod = helper.design.top_module()
        cell_types = [c.type.str() for c in mod.selected_cells()]
        assert any("exact_alu_msb" in ct for ct in cell_types)
