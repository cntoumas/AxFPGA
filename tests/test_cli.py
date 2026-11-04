"""CLI (axfpga.flow) argument handling, the generic evaluator, and the pipeline's formal wrappers."""

import json
from pathlib import Path

import pytest

from axfpga import flow
from axfpga.pipeline import GenericEvaluator, _formal_control

from test_formal_control import EXACT, TOP, _module


class _FakeFlow:
    kwargs = {}

    def __init__(self, **kw):
        _FakeFlow.kwargs = kw

    def run(self):
        return {"flow_status": "SUCCESS"}


@pytest.fixture
def fake_flow(monkeypatch):
    monkeypatch.setattr(flow, "AxcFlow", _FakeFlow)
    return _FakeFlow


def test_multi_file_top_out_dir_and_dsp(fake_flow, capsys):
    flow.main(["-i", "a.v", "b.v", "--top", "t", "--out-dir", "o", "--dsp", "off", "-b", "7"])
    kw = fake_flow.kwargs
    assert kw["input_verilog"] == ["a.v", "b.v"]
    assert kw["top"] == "t" and kw["output_dir"] == "o" and kw["dsp"] is False and kw["override_bound"] == 7.0
    assert json.loads(capsys.readouterr().out)["flow_status"] == "SUCCESS"


def test_dsp_default_on_and_aliases(fake_flow):
    flow.main(["-i", "a.v"])
    assert fake_flow.kwargs["dsp"] is True and fake_flow.kwargs["output_dir"] == "build/axc_output"
    flow.main(["-i", "a.v", "-o", "x", "--nodsp"])
    assert fake_flow.kwargs["dsp"] is False and fake_flow.kwargs["output_dir"] == "x"


def test_failed_flow_exits_nonzero(monkeypatch):
    class Stopped(_FakeFlow):
        def run(self):
            return {"flow_status": "STOPPED"}
    monkeypatch.setattr(flow, "AxcFlow", Stopped)
    with pytest.raises(SystemExit) as e:
        flow.main(["-i", "a.v"])
    assert e.value.code == 1


def test_generic_evaluator_modular_error():
    ev = GenericEvaluator([("x", 8)], width=8, signed=True)
    exact = [{"y": 0xFF}, {"y": 10}, {"y": 0}]
    approx = [{"y": 0x01}, {"y": 14}, {"y": 0}]      # -1 vs 1 wraps to distance 2
    st = ev.error_stats("t", exact, approx, "y", 0)
    assert st["wce"] == 4.0 and st["med"] == 2.0 and st["er"] == pytest.approx(2 / 3)
    vecs, _ = ev.build_stimulus("t")
    assert vecs == ev.build_stimulus("t")[0] and len(vecs) == ev.n_vectors     # seeded


def test_cycle1_control_mismatch_is_falsified_through_pipeline(tmp_path):
    # ctrl differs only in the first time step (powers up 0, set by the first edge)
    approx = _module("assign ctrl = r[0] | ~started;",
                     extra_regs="reg started; always @(posedge clk) started <= 1'b1;")
    e, a, m = tmp_path / "e.v", tmp_path / "a.v", tmp_path / "m.json"
    e.write_text(EXACT)
    a.write_text(approx)
    m.write_text(json.dumps({"top_module": TOP, "relaxed_signals": [{"wire": "data", "bound": 4.0}]}))
    assert _formal_control(e, a, m) == "FALSIFIED"
    assert _formal_control(e, e, m) == "PROVEN"
