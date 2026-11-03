"""Unit tests for Plan 1.3: FPGA-Aware Surrogate Cost in MOP & Post-Rewriting Vivado Flow."""

import tempfile
from pathlib import Path
import pytest
from pyosys import libyosys as ys

from axfpga.pyals_ext import dead_cells, fpga_cost, prep_design, remap_xilinx_7series
from pyalslib import YosysHelper, ALSGraph
from axfpga.vivado import VivadoBatchRunner, resolve_part

REPO_ROOT = Path(__file__).resolve().parent.parent
MAC_CONE = REPO_ROOT / "benchmarks" / "mac" / "mac.v"


def test_alsgraph_dead_cells_and_fpga_cost():
    """Verify ALSGraph dead cells detection and FPGA surrogate cost calculation."""
    vlog = """
    module test_logic (
        input a, b, c, d,
        output y
    );
        wire w1 = a & b;
        wire w2 = c | d;
        assign y = w1 ^ w2;
    endmodule
    """
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w") as f:
        f.write(vlog)
        f.flush()

        helper = YosysHelper()
        helper.read_sources(f.name, "test_logic")
        prep_design(helper, cut_size=4, protect_carry_chains=False)

        graph = ALSGraph(helper.design)
        cells = graph.get_cells()
        assert len(cells) > 0

        # Baseline cost with exact spec (no configuration overrides)
        base_cost = fpga_cost(graph, configuration={})
        assert base_cost > 0.0

        # Dead cells with no drops should be empty
        dead = dead_cells(graph, configuration={})
        assert len(dead) == 0


def test_vivado_part_resolution_and_tcl_generation(tmp_path: Path):
    """Verify Vivado batch TCL script generation for Zybo (xc7z010) and PYNQ (xc7z020)."""
    assert resolve_part("zybo") == "xc7z010clg400-1"
    assert resolve_part("pynq") == "xc7z020clg400-1"
    assert resolve_part("pynq-z2") == "xc7z020clg400-1"
    assert resolve_part("artix7") == "xc7a35ticsg324-1L"

    # Test Zybo runner
    zybo_runner = VivadoBatchRunner(part="zybo")
    tcl_out = tmp_path / "zybo_synth.tcl"
    vlog_file = tmp_path / "top.v"
    vlog_file.write_text("module top(input a, output y); assign y = ~a; endmodule", encoding="utf-8")

    gen_path = zybo_runner.generate_tcl(
        top_module="top",
        verilog_files=[vlog_file],
        out_tcl_path=tcl_out,
    )
    assert gen_path.exists()
    content = gen_path.read_text(encoding="utf-8")

    # Enforce Action Item 2 requirements
    assert "synth_design -top top -part xc7z010clg400-1 -flatten_hierarchy full -directive AreaOptimized_high" in content
    assert "opt_design -directive ExploreArea" in content
    assert "report_utilization -file utilization.rpt" in content


def test_vivado_report_parsing():
    """Verify Vivado utilization report parsing for Slice LUTs and CARRY4."""
    mock_rpt = """
1. Slice Logic
--------------

+-------------------------+------+-------+-----------+-------+
|        Site Type        | Used | Fixed | Available | Util% |
+-------------------------+------+-------+-----------+-------+
| Slice LUTs              |  142 |     0 |     17600 |  0.81 |
|   LUT as Logic          |  142 |     0 |     17600 |  0.81 |
| Slice Registers         |   48 |     0 |     35200 |  0.14 |
| CARRY4                  |   12 |     0 |      4400 |  0.27 |
| DSP48E1                 |    2 |     0 |        80 |  2.50 |
+-------------------------+------+-------+-----------+-------+
"""
    with tempfile.NamedTemporaryFile(suffix=".rpt", mode="w") as f:
        f.write(mock_rpt)
        f.flush()

        metrics = VivadoBatchRunner.parse_utilization_report(f.name)
        assert metrics["slice_luts"] == 142
        assert metrics["carry4"] == 12
        assert metrics["dsp48e1"] == 2
        # Estimated logic cells = 142 + 2*12 + 100*2 = 142 + 24 + 200 = 366
        assert metrics["estimated_lcs"] == 366


def test_alsrewriter_xilinx_remap():
    """Verify remap_xilinx_7series (pyals_ext) executes cleanly."""
    vlog = """
    module small (input a, b, output y);
        assign y = a ^ b;
    endmodule
    """
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w") as f:
        f.write(vlog)
        f.flush()

        helper = YosysHelper()
        helper.read_sources(f.name, "small")
        prep_design(helper, cut_size=4, protect_carry_chains=False)

        # Should execute opt_clean; opt; synth_xilinx -family xc7 -noiopad -run begin:check; opt_clean without error
        remap_xilinx_7series(helper.design)
