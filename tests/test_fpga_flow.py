"""Integration test for end-to-end FPGA flow (Xilinx 7-Series / Artix-7)."""

from pathlib import Path
import pytest
from axfpga.flow import AxcFlow

REPO_ROOT = Path(__file__).resolve().parent.parent
MAC_VLOG = REPO_ROOT / "benchmarks" / "mac" / "mac.v"


def test_fpga_end_to_end_flow_mac(tmp_path: Path):
    out_dir = tmp_path / "mac_fpga_run"

    flow = AxcFlow(
        input_verilog=MAC_VLOG,
        output_dir=out_dir,
        fpga_family="xc7",
        fpga_device="xc7a35t",
        fpga_nodsp=True,
        clk_port="clk",
        clk_period_ns=10.0,
        sim_samples=100,
        verify_cycles=100,
        axc_method="pyals",
        override_bound=64.0,
        enable_formal=True,
        use_realistic_traces=True,
    )

    summary = flow.run()

    assert summary["flow_status"] in ("SUCCESS", "NO_APPROXIMATION")
    assert summary["target"] == "fpga"
    assert summary["dsp"] is False
    assert set(summary["result"]["formal"]) == {"control_equivalence", "wce_bound"}
    assert summary["formal_control_status"] and summary["formal_wce_status"]
    if summary["flow_status"] == "SUCCESS":
        assert summary["formal_control_status"] == "PROVEN"

    fpga = summary["fpga"]
    assert fpga["family"] == "xc7"
    assert fpga["device"] == "xc7a35t"
    assert fpga["exact"]["luts"] > 0
    assert fpga["approx"]["luts"] > 0
    assert fpga["exact"]["carry4"] > 0
    assert (out_dir / "flow_summary.json").exists()
