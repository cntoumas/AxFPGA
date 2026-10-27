"""Unit tests for RTL Loop, Tap, and Stride Perforator."""

from pathlib import Path
import pytest
from axfpga.perforator import RtlLoopPerforator

REPO_ROOT = Path(__file__).resolve().parent.parent
FIR_PATH = REPO_ROOT / "benchmarks" / "fir" / "fir.v"


def test_spatial_tap_perforation_fir(tmp_path: Path):
    """Test spatial tap dropping on 4-tap FIR filter."""
    perforator = RtlLoopPerforator(verilog_path=FIR_PATH, work_dir=tmp_path)
    trees = perforator.detect_spatial_sum_trees()

    assert len(trees) >= 1
    sum_tree = next(t for t in trees if t.target_signal == "sum")
    assert len(sum_tree.terms) == 4
    assert sum_tree.terms == ["p0", "p1", "p2", "p3"]

    # Drop 1 tap
    p_drop1 = perforator.perforate_spatial_tree(sum_tree, num_taps_to_drop=1)
    assert p_drop1.exists()
    content = p_drop1.read_text(encoding="utf-8")
    assert "p0 + p1 + p2" in content
    assert "p0 + p1 + p2 + p3" not in content

    # Drop 2 taps
    p_drop2 = perforator.perforate_spatial_tree(sum_tree, num_taps_to_drop=2)
    assert p_drop2.exists()
    content2 = p_drop2.read_text(encoding="utf-8")
    assert "p0 + p1" in content2


def test_sequential_counter_perforation(tmp_path: Path):
    """Test early termination on sequential iterative counter."""
    seq_code = """
    module iter_calc (
        input wire clk,
        input wire rst_n,
        input wire start,
        output reg done,
        output reg [15:0] result
    );
        reg [3:0] count;
        always @(posedge clk or negedge rst_n) begin
            if (!rst_n) begin
                count <= 4'd0;
                done <= 1'b0;
                result <= 16'd0;
            end else if (start) begin
                if (count == 4'd12) begin
                    done <= 1'b1;
                end else begin
                    count <= count + 1'b1;
                    result <= result + count;
                end
            end
        end
    endmodule
    """
    v_file = tmp_path / "iter_calc.v"
    v_file.write_text(seq_code, encoding="utf-8")

    perforator = RtlLoopPerforator(verilog_path=v_file, work_dir=tmp_path)
    counters = perforator.detect_sequential_counters()
    assert len(counters) >= 1
    cnt = counters[0]
    assert cnt.counter_name == "count"
    assert cnt.bound_value == 12

    p_mut = perforator.perforate_sequential_counter(cnt, cycle_reduction=2)
    assert p_mut.exists()
    mut_text = p_mut.read_text(encoding="utf-8")
    assert "4'd10" in mut_text


def test_parameterized_sequential_counter_perforation(tmp_path: Path):
    """Test early termination on parameterized sequential loop counter."""
    param_seq_code = """
    module agu_loop #(
        parameter N = 1024,
        parameter LOG2_N = 10
    )(
        input wire clk,
        input wire rst,
        output reg done
    );
        reg [LOG2_N-2:0] bfy_count;
        reg [3:0] stage_idx;

        always @(posedge clk) begin
            if (rst) begin
                bfy_count <= 0;
                stage_idx <= 0;
                done <= 0;
            end else begin
                if (bfy_count == (N/2) - 1) begin
                    bfy_count <= 0;
                    stage_idx <= stage_idx + 1;
                    if (stage_idx == LOG2_N - 1) begin
                        done <= 1;
                    end
                end else begin
                    bfy_count <= bfy_count + 1;
                end
            end
        end
    endmodule
    """
    v_file = tmp_path / "agu_loop.v"
    v_file.write_text(param_seq_code, encoding="utf-8")

    perforator = RtlLoopPerforator(verilog_path=v_file, work_dir=tmp_path)
    counters = perforator.detect_sequential_counters()
    assert len(counters) >= 2

    stage_loop = next((c for c in counters if c.counter_name == "stage_idx"), None)
    assert stage_loop is not None
    assert not stage_loop.is_numeric
    assert stage_loop.bound_value == "LOG2_N - 1"

    bfy_loop = next((c for c in counters if c.counter_name == "bfy_count"), None)
    assert bfy_loop is not None
    assert not bfy_loop.is_numeric
    assert bfy_loop.bound_value == "(N/2) - 1"

    # Perforate stage loop by 2 stages (e.g. 8-stage FFT)
    p_stage2 = perforator.perforate_sequential_counter(stage_loop, cycle_reduction=2)
    assert p_stage2.exists()
    assert "stage_idx == LOG2_N - 3" in p_stage2.read_text(encoding="utf-8")

    # Perforate butterfly loop by 1
    p_bfy = perforator.perforate_sequential_counter(bfy_loop, cycle_reduction=1)
    assert p_bfy.exists()
    assert "bfy_count == (N/2) - 2" in p_bfy.read_text(encoding="utf-8")


def test_temporal_stride_decimation(tmp_path: Path):
    """Test temporal decimation wrapper on streaming interface."""
    perforator = RtlLoopPerforator(verilog_path=FIR_PATH, work_dir=tmp_path)
    p_decim = perforator.perforate_temporal_decimation(stride=2)
    assert p_decim.exists()
    content = p_decim.read_text(encoding="utf-8")
    assert "_axc_stride_counter" in content
    assert "_axc_stride_enable" in content
