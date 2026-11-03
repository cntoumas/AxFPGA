"""The pinned Yosys command line (axfpga.yosys_cli over the pyosys wheel)."""

import subprocess

from axfpga.config import xilinx_cells_sim, yosys_abc, yosys_cmd, yosys_share_dir

ADDER = "module t(input [3:0] a, b, output [4:0] y); assign y = a + b; endmodule\n"


def run(args, cwd=None):
    return subprocess.run(yosys_cmd() + args, capture_output=True, text=True, cwd=cwd)


def test_version_is_pinned_070():
    res = run(["-V"])
    assert res.returncode == 0
    assert res.stdout.startswith("Yosys 0.70")


def test_share_dir_and_abc_belong_to_the_wheel():
    assert xilinx_cells_sim().is_file()
    assert yosys_abc().is_file()
    assert "pyosys" in str(yosys_share_dir())


def test_commands_log_to_stdout(tmp_path):
    (tmp_path / "t.v").write_text(ADDER)
    res = run(["-p", "read_verilog t.v; synth -top t; ltp"], cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "Longest topological path in t" in res.stdout


def test_quiet_suppresses_log(tmp_path):
    (tmp_path / "t.v").write_text(ADDER)
    res = run(["-q", "-p", "read_verilog t.v; synth -top t"], cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""


def test_script_file_and_input_file(tmp_path):
    (tmp_path / "t.v").write_text(ADDER)
    (tmp_path / "s.ys").write_text("synth -top t\nwrite_verilog -noattr out.v\n")
    res = run(["-q", "-s", "s.ys", "t.v"], cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "module t" in (tmp_path / "out.v").read_text()


def test_errors_give_exit_code_1_and_stderr(tmp_path):
    res = run(["-q", "-p", "read_verilog does_not_exist.v"], cwd=tmp_path)
    assert res.returncode == 1
    assert "does_not_exist.v" in res.stderr


def test_sat_result_strings_match_what_the_verifier_parses(tmp_path):
    (tmp_path / "c.v").write_text(
        "module c(input [3:0] a, output ok, output bad); assign ok = (a + 4'd0) == a; assign bad = a == 4'd3; endmodule\n")
    res = run(["-p", "read_verilog c.v; proc; sat -prove ok 1; sat -prove bad 0; sat -tempinduct -prove ok 1 -seq 1"],
              cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "SAT proof finished - no model found: SUCCESS!" in res.stdout
    assert "SAT proof finished - model found: FAIL!" in res.stdout
    assert "Induction step proven: SUCCESS!" in res.stdout
