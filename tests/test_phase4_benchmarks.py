"""Phase 4 benchmark set: suite entries, attributes, plugins, reference models, DSP report. No HDL simulation.

The RTL-against-model checks that need iverilog are in tests/test_phase4_rtl.py.
"""

import importlib.util
import math
import random
import sys
from pathlib import Path

import numpy as np
import pytest

from axfpga.config import axbench_root
from axfpga.quality import QualitySpec, make_evaluator
from axfpga.quality.attrs import parse_wire_attrs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase4_models as M  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "benchmarks" / "axbench"
HAVE_AXBENCH = (axbench_root() / "scripts" / "images" / "peppers.png").exists()
needs_axbench = pytest.mark.skipif(not HAVE_AXBENCH, reason="AxBench checkout (AXFPGA_AXBENCH_ROOT) not available")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ANN_MOD = _load(PLUGIN_DIR / "annotations.py", "axbench_ann_p4")
ANN = ANN_MOD.ANNOTATIONS

# id: (top, relaxed wire, relaxed width, data inputs in port order, file)
NEW = {
    "conv2d_3x3": ("conv2d_3x3", "act_out", 8, [("in_valid", 1), ("win", 216), ("weights", 216)], "conv2d/conv2d_3x3.v"),
    "fft64_psd": ("fft64_psd", "out_pow", 32, [("in_valid", 1), ("in_re", 16), ("in_im", 16)], "fft64/fft64_psd.v"),
    "image_pipeline": ("image_pipeline", "edge_out", 8, [("in_valid", 1), ("rgb_in", 24)], "image_pipeline/image_pipeline.v"),
    "iir_biquad": ("iir_biquad", "y_out", 16, [("in_valid", 1), ("x_in", 16)], "iir/iir_biquad.v"),
}
BHV = ["multiplier_16b_bhv", "multiplier_32b_bhv", "neural_network_bhv", "kinematics_fwd_bhv", "kinematics_inv_bhv",
       "sobel_npu_bhv"]


@pytest.fixture(scope="module")
def suite():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import run_pyals_yosys_suite as s
    finally:
        sys.path.pop(0)
    return s


# ------------------------------------------------------------------------------------------- suite entries
def test_phase4_group_lists_variants_and_new_designs(suite):
    ids = [b["id"] for b in suite.define_benchmark_suite() if b["group"] == "phase4"]
    assert ids == BHV + list(NEW)
    assert [b["id"] for b in suite.select_suite(None, "axbench")] == \
        [b["id"] for b in suite.define_benchmark_suite() if b["group"] == "axbench" and b["id"] not in suite.EXCLUDED]
    assert "sobel_npu_bhv" in [b["id"] for b in suite.select_suite(None, "phase4")]


def test_behavioural_variant_swaps_only_the_multiplier(suite):
    by = {b["id"]: b for b in suite.define_benchmark_suite()}
    for vid in BHV:
        orig, var = by[vid[:-4]], by[vid]
        changed = [(a, b) for a, b in zip(orig["files"], var["files"]) if a != b]
        assert len(changed) == 1 and len(orig["files"]) == len(var["files"])
        a, b = changed[0]
        assert b == suite.BEHAVIOURAL_MULT[a.name] and b.exists()
        for k in ("top", "relax_wire", "precise_signals", "bound", "quality_attrs"):
            assert var[k] == orig[k], (vid, k)
    assert by["sobel_npu_bhv"]["patches"] == by["sobel_npu"]["patches"] != []
    assert by["sobel_npu_bhv"]["device"] == by["sobel_npu"]["device"] == "xc7z020"


def test_behavioural_multiplier_keeps_module_interface():
    for f, top, w in (("Multiplier_15_0_1000_bhv.v", "Multiplier_15_0_1000", 16),
                      ("Multiplier_31_0_3000_bhv.v", "Multiplier_31_0_3000", 32)):
        txt = (ROOT / "benchmarks" / "behavioural" / f).read_text()
        assert f"module {top}(P, IN1, IN2, clk, rst);" in txt
        assert f"output [{2 * w - 1}:0] P;" in txt and f"input [{w - 1}:0] IN1;" in txt
        assert "$signed(IN1) * $signed(IN2)" in txt and "if (rst)" in txt


def test_new_designs_have_files_plugins_and_bounds(suite):
    by = {b["id"]: b for b in suite.define_benchmark_suite()}
    for bid, (top, wire, width, _, f) in NEW.items():
        b = by[bid]
        assert b["files"] == [ROOT / "benchmarks" / f] and b["files"][0].exists()
        assert b["top"] == top and b["relax_wire"] == wire
        assert b["bound"] == QualitySpec.parse(ANN[bid]["quality"], wire).wce_bound > 0
        assert (b["files"][0].parent / ANN[bid]["plugin"]).exists()
        assert b["files"][0].parent in b["search_dirs"]


def test_annotations_parse_and_reserved_horizon():
    for bid, (top, wire, width, _, _) in NEW.items():
        a = ANN[bid]
        raw = {"axc_format": a["format"], "axc_quality": a["quality"], "axc_plugin": a["plugin"], "axc_seed": a["seed"]}
        if "horizon" in a:
            raw["axc_horizon"] = a["horizon"]
        wq = parse_wire_attrs(raw, wire, width)
        assert wq.wce_bound > 0 and str(wq.fmt) == a["format"]
        if "horizon" in a:
            assert wq.reserved["horizon"] == a["horizon"]
    for vid in BHV:
        assert ANN[vid] == ANN[vid[:-4]]


def test_annotate_verilog_marks_new_designs(tmp_path, suite):
    from axfpga.pipeline import annotate_verilog
    for bid, b in ((b["id"], b) for b in suite.define_benchmark_suite() if b["id"] in NEW):
        out = annotate_verilog(b["files"], tmp_path / bid, b["top"], b["relax_wire"], b["precise_signals"],
                               b["bound"], attrs=b["quality_attrs"])
        txt = out[0].read_text()
        assert f'output [{NEW[bid][2] - 1}:0] {b["relax_wire"]}' in txt
        assert "(* axc_relax" in txt and f'axc_quality="{ANN[bid]["quality"]}"' in txt
        for p in ("clk", "rst", "in_valid"):
            assert f"(* axc_precise *) input {p}" in txt, (bid, p)
        if "horizon" in ANN[bid]:
            assert f"axc_horizon={ANN[bid]['horizon']}" in txt


def test_new_designs_elaborate_with_expected_ports():
    from axfpga.pipeline import _top_ports
    for bid, (top, wire, width, inputs, f) in NEW.items():
        p = _top_ports([ROOT / "benchmarks" / f], top)
        assert p["clk"] == "clk" and p["rst"] == "rst" and not p["rst_active_low"]
        assert sorted(p["inputs"]) == sorted(inputs)
        assert (wire, width) in p["outputs"] and ("out_valid", 1) in p["outputs"]


# ------------------------------------------------------------------------------------------- plugins
def _evaluator(bid):
    top, wire, width, inputs, f = NEW[bid]
    a = ANN[bid]
    wq = parse_wire_attrs({"axc_format": a["format"], "axc_quality": a["quality"], "axc_plugin": a["plugin"],
                           "axc_seed": a["seed"]}, wire, width)
    return make_evaluator(wq, design_id=bid, top=top, inputs=inputs, outputs=[("out_valid", 1), (wire, width)],
                          has_clock=True, base_dir=(ROOT / "benchmarks" / f).parent, search_dirs=[PLUGIN_DIR])


@needs_axbench
@pytest.mark.parametrize("bid", list(NEW))
def test_plugin_streams_fit_ports_and_differ(bid):
    ev = _evaluator(bid)
    widths = dict(NEW[bid][3])
    t, tn = ev.build_stimulus(bid)
    h, hn = ev.build_heldout(bid)
    assert t == ev.build_stimulus(bid)[0] and h == ev.build_heldout(bid)[0]
    assert t != h and tn["note"] and hn["note"]
    for vecs in (t, h):
        assert len(vecs) >= 1000
        for v in vecs:
            assert set(v) == set(widths) and v["in_valid"] == 1
            for k, x in v.items():
                assert -(1 << (widths[k] - 1)) <= x < (1 << widths[k]), (k, x)


@needs_axbench
@pytest.mark.parametrize("bid", list(NEW))
def test_plugin_metric_identical_and_perturbed(bid):
    ev = _evaluator(bid)
    wire, width = NEW[bid][1], NEW[bid][2]
    vecs, _ = ev.build_stimulus(bid)
    rnd = random.Random(3)
    lim = (1 << (width - 1)) if bid == "iir_biquad" else (1 << width)
    exact = [{wire: rnd.randrange(lim), "out_valid": 1} for _ in vecs]
    same = ev.app_metrics(bid, vecs, exact, [dict(r) for r in exact], relax_wire=wire, skip=16)
    assert same["value"] == math.inf and same["pass"] is True
    approx = [dict(r) for r in exact]
    for r in approx[::2]:
        r[wire] ^= 1
    worse = ev.app_metrics(bid, vecs, exact, approx, relax_wire=wire, skip=16)
    assert math.isfinite(worse["value"])


@needs_axbench
def test_fft_plugin_respects_input_limit():
    ev = _evaluator("fft64_psd")
    for vecs in (ev.build_stimulus("fft64_psd")[0], ev.build_heldout("fft64_psd")[0]):
        assert max(max(abs(v["in_re"]), abs(v["in_im"])) for v in vecs) <= 1 << 14


# ------------------------------------------------------------------------------------------- reference models
def test_fft64_model_matches_numpy():
    rng = np.random.default_rng(1)
    x = rng.integers(-(1 << 13), 1 << 13, size=(3, 64)) + 1j * rng.integers(-(1 << 13), 1 << 13, size=(3, 64))
    stream = [(int(v.real), int(v.imag)) for v in x.flatten()] + [(0, 0)] * 128
    out = M.fft64_model(stream)
    assert len(out) >= 3 * 64
    for blk in range(3):
        ref = np.abs(np.fft.fft(x[blk]) / 64)
        got = np.zeros(64)
        for j in range(64):
            got[M.bitrev6(j)] = math.sqrt(out[blk * 64 + j])
        assert np.max(np.abs(got - ref)) <= 4.0, blk


def test_fft64_model_tone_lands_on_its_bin():
    n = np.arange(64)
    x = 6000 * np.exp(2j * np.pi * 5 * n / 64)
    out = M.fft64_model([(int(round(v.real)), int(round(v.imag))) for v in x] + [(0, 0)] * 64)
    j = int(np.argmax(out[:64]))
    assert M.bitrev6(j) == 5 and abs(math.sqrt(out[j]) - 6000) <= 4


def test_iir_model_tracks_float_filter():
    rng = np.random.default_rng(2)
    x = np.clip(np.round(6000 * np.sin(2 * np.pi * 0.01 * np.arange(2000)) + rng.normal(0, 300, 2000)), -16384, 16384)
    y = M.iir_model([int(v) for v in x])
    b = np.array(M.IIR_B) / 16384.0
    a = np.array(M.IIR_A) / 16384.0
    yf = np.zeros(len(x))
    for i in range(len(x)):
        yf[i] = (b[0] * x[i] + (b[1] * x[i - 1] if i > 0 else 0) + (b[2] * x[i - 2] if i > 1 else 0)
                 - (a[1] * yf[i - 1] if i > 0 else 0) - (a[2] * yf[i - 2] if i > 1 else 0))
    assert sum(M.IIR_B) == sum(M.IIR_A)                       # DC gain 1
    assert np.max(np.abs(np.array(y) - yf)) <= 16             # floor rounding, amplified by the feedback (<= 12.5x)
    assert max(abs(v) for v in y) < 32767                     # no saturation for |x| <= 2^14


def test_image_pipeline_model_matches_direct_computation():
    w = 16
    rng = np.random.default_rng(4)
    img = rng.integers(0, 256, size=(w, w, 3))
    m = M.ImagePipelineModel(w)
    pix = [(int(p[0]) << 16) | (int(p[1]) << 8) | int(p[2]) for row in img for p in row]
    out = [m.step(v) for v in pix + [0] * (2 * w + 16)]
    luma = (77 * img[..., 0] + 150 * img[..., 1] + 29 * img[..., 2]) >> 8
    k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]])
    blur = np.zeros_like(luma)
    for r in range(1, w - 1):
        for c in range(1, w - 1):
            blur[r, c] = int((luma[r - 1:r + 2, c - 1:c + 2] * k).sum()) >> 4
    for r in range(2, w - 2):
        for c in range(2, w - 2):
            win = blur[r - 1:r + 2, c - 1:c + 2]
            gx = int(win[:, 2].dot([1, 2, 1]) - win[:, 0].dot([1, 2, 1]))
            gy = int(win[2, :].dot([1, 2, 1]) - win[0, :].dot([1, 2, 1]))
            ref = min(255, (abs(gx) + abs(gy)) >> 2)
            t = r * w + c + 2 * w + M.IMAGE_LATENCY_EXTRA
            assert out[t] == ref, (r, c)


def test_conv2d_model_and_plugin_weights():
    p = _load(ROOT / "benchmarks" / "conv2d" / "conv2d_plugin.py", "conv2d_plugin_t") if HAVE_AXBENCH else None
    if p is not None:
        assert len(p.WEIGHTS) == 27 and all(-128 <= w <= 127 for w in p.WEIGHTS)
        assert abs(sum(p.WEIGHTS) - (1 << p.SHIFT)) <= 3          # kernel sums to ~1.0 (rounding of 27 weights)
    assert M.conv2d_act([255] * 27, [127] * 27) == 255
    assert M.conv2d_act([255] * 27, [-1] * 27) == 0
    assert M.conv2d_act([64] * 9 + [0] * 18, [1] * 27) == (64 * 9) >> 6


# ------------------------------------------------------------------------------------------- DSP report, estimator
def _rec(bid, dsps, luts, lc, saved, nodsp):
    u = {"dsps": dsps, "luts": luts, "logic_cells": lc}
    return {"id": bid, "name": bid, "exact": u, "approx": dict(u, logic_cells=lc - 1), "savings_pct": {"logic_cells": saved},
            "formal": {"control_equivalence": "PROVEN", "wce_bound": "PROVEN"}, "fell_back": False,
            "certification": {"tier": "certified"}, "dsp_inference": not nodsp}


def test_dsp_comparison_gate_checks(tmp_path, suite):
    on = [_rec("multiplier_16b_bhv", 1, 10, 10, 5.0, False), _rec("iir_biquad", 5, 100, 120, 1.0, False),
          _rec("neural_network_bhv", 0, 300, 300, 0.0, False)]
    off = [_rec("multiplier_16b_bhv", 0, 300, 320, 5.0, True), _rec("iir_biquad", 0, 900, 950, 1.0, True)]
    fails = suite.export_dsp_comparison(on, off, tmp_path)
    assert any(f.startswith("neural_network_bhv: no result with DSP inference off") for f in fails)
    assert any("neural_network_bhv" in f for f in fails)
    assert not any(f.startswith("multiplier_16b_bhv") or f.startswith("iir_biquad") for f in fails)
    md = (tmp_path / "dsp_comparison.md").read_text()
    assert "FAIL" in md and "| 1 -> 1 |" in md
    off[0]["exact"]["dsps"] = 2
    on.pop()
    fails = suite.export_dsp_comparison(on, off, tmp_path)
    assert fails == ["multiplier_16b_bhv: 2 DSP48E1 with DSP inference off"]


def test_dsp_runs_layout(suite, tmp_path):
    assert suite._dsp_runs("on", tmp_path / "o", tmp_path / "b") == [(False, tmp_path / "o", tmp_path / "b")]
    assert suite._dsp_runs("off", tmp_path / "o", tmp_path / "b") == [(True, tmp_path / "o", tmp_path / "b")]
    assert suite._dsp_runs("both", tmp_path / "o", tmp_path / "b") == [
        (False, tmp_path / "o" / "dsp_on", tmp_path / "b" / "dsp_on"),
        (True, tmp_path / "o" / "dsp_off", tmp_path / "b" / "dsp_off")]


def test_estimator_device_fields_follow_the_estimator():
    from axfpga.estimator import FpgaEstimator
    res = {"total_luts": 17600, "ffs": 3520, "dsps": 8, "ramb36": 6, "ramb18": 0, "device": "xc7z010",
           "lut_utilization_pct": 100.0}
    small = FpgaEstimator(family="xc7", device="xc7z010")._device_fields(dict(res))
    big = FpgaEstimator(family="xc7", device="xc7z020")._device_fields(dict(res))
    assert small["lut_utilization_pct"] == 100.0 and small["dsp_utilization_pct"] == 10.0
    assert big["device"] == "xc7z020" and big["lut_utilization_pct"] == round(17600 / 53200 * 100, 3)
    assert big["dsp_utilization_pct"] == round(8 / 220 * 100, 3)
