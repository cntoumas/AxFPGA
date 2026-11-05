"""Phase 4 RTL against bit-accurate models (iverilog, cycle by cycle).

- benchmarks/behavioural/*: the behavioural multipliers equal the AxBench array multipliers (and the exact product).
- conv2d_3x3, iir_biquad, image_pipeline, fft64_psd: the RTL equals tests/phase4_models.py on random streams.
The models themselves are checked against floating-point references in tests/test_phase4_benchmarks.py.
"""

import random
import shutil
import sys
from pathlib import Path

import pytest

from axfpga.config import axbench_root

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase4_models as M  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmarks"

pytestmark = pytest.mark.skipif(shutil.which("iverilog") is None or shutil.which("vvp") is None,
                                reason="iverilog not installed")


def _sim(files, top, inputs, outputs, vectors, work, tag):
    from axfpga.activity_power import run_vector_sim
    return run_vector_sim([str(f) for f in files], top=top, inputs=inputs, outputs=outputs, vectors=vectors,
                          work_dir=work, clk="clk", rst="rst", tag=tag)["outputs"]


@pytest.mark.parametrize("w, orig, bhv, top", [
    (16, "multiplier.16b/rtl/Multiplier_15_0_1000.v", "Multiplier_15_0_1000_bhv.v", "Multiplier_15_0_1000"),
    (32, "multiplier.32b/rtl/Multiplier_31_0_3000.v", "Multiplier_31_0_3000_bhv.v", "Multiplier_31_0_3000"),
])
def test_behavioural_multiplier_equals_array_multiplier(tmp_path, w, orig, bhv, top):
    src = axbench_root() / "circuits" / orig
    if not src.exists():
        pytest.skip("AxBench checkout (AXFPGA_AXBENCH_ROOT) not available")
    rnd = random.Random(w)
    edge = [0, 1, (1 << (w - 1)) - 1, 1 << (w - 1), (1 << w) - 1]
    vecs = [{"IN1": a, "IN2": b} for a in edge for b in edge]
    vecs += [{"IN1": rnd.getrandbits(w), "IN2": rnd.getrandbits(w)} for _ in range(400)]
    io = dict(inputs=[("IN1", w), ("IN2", w)], outputs=[("P", 2 * w)], vectors=vecs, work=tmp_path)
    a = _sim([src], top, tag="array", **io)
    b = _sim([BENCH / "behavioural" / bhv], top, tag="bhv", **io)
    assert a == b
    for v, r in zip(vecs, b):            # one register stage: row i holds the product of vector i
        assert M.wrap(r["P"], 2 * w) == M.wrap(v["IN1"], w) * M.wrap(v["IN2"], w)


def test_conv2d_rtl_matches_model(tmp_path):
    rnd = random.Random(7)
    vecs, refs = [], []
    for i in range(300):
        win = [rnd.randrange(256) for _ in range(27)]
        wts = [rnd.randrange(-128, 128) for _ in range(27)]
        if i % 50 == 0:
            wts = [127] * 27                 # saturation
        if i % 50 == 1:
            wts = [-128] * 27                # ReLU
        vecs.append({"in_valid": 1, "win": sum(p << (8 * k) for k, p in enumerate(win)),
                     "weights": sum((x & 255) << (8 * k) for k, x in enumerate(wts))})
        refs.append(M.conv2d_act(win, wts))
    rows = _sim([BENCH / "conv2d" / "conv2d_3x3.v"], "conv2d_3x3",
                [("in_valid", 1), ("win", 216), ("weights", 216)], [("out_valid", 1), ("act_out", 8)], vecs, tmp_path, "conv")
    for i in range(len(vecs) - 1):          # two register stages: row i + 1 holds the activation of vector i
        assert rows[i + 1]["act_out"] == refs[i], i
        assert rows[i + 1]["out_valid"] == 1


def test_iir_rtl_matches_model(tmp_path):
    rnd = random.Random(8)
    x = [rnd.randrange(-(1 << 14), 1 << 14) for _ in range(600)] + [16384] * 200 + [-16384] * 200
    rows = _sim([BENCH / "iir" / "iir_biquad.v"], "iir_biquad", [("in_valid", 1), ("x_in", 16)],
                [("out_valid", 1), ("y_out", 16)], [{"in_valid": 1, "x_in": v} for v in x], tmp_path, "iir")
    ref = M.iir_model(x)
    assert [M.wrap(r["y_out"], 16) for r in rows] == ref


def test_image_pipeline_rtl_matches_model(tmp_path):
    rnd = random.Random(9)
    pix = [rnd.getrandbits(24) for _ in range(64 * 12)] + [0] * 300
    rows = _sim([BENCH / "image_pipeline" / "image_pipeline.v"], "image_pipeline", [("in_valid", 1), ("rgb_in", 24)],
                [("out_valid", 1), ("edge_out", 8)], [{"in_valid": 1, "rgb_in": p} for p in pix], tmp_path, "img")
    m = M.ImagePipelineModel(64)
    assert [r["edge_out"] for r in rows] == [m.step(p) for p in pix]


def test_fft64_rtl_matches_model(tmp_path):
    rnd = random.Random(10)
    lim = 1 << 14
    stream = [(rnd.randrange(-lim, lim + 1), rnd.randrange(-lim, lim + 1)) for _ in range(4 * 64)] + [(0, 0)] * 160
    vecs = [{"in_valid": 1, "in_re": a, "in_im": b} for a, b in stream]
    rows = _sim([BENCH / "fft64" / "fft64_psd.v"], "fft64_psd", [("in_valid", 1), ("in_re", 16), ("in_im", 16)],
                [("out_valid", 1), ("out_pow", 32)], vecs, tmp_path, "fft")
    got = M.fft64_ports(rows)
    ref = M.fft64_model(stream)
    assert len(got) >= 4 * 64
    assert got == ref[:len(got)]
