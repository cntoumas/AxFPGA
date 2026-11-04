"""The AxBench plugins + attributes reproduce scripts/axbench_apps.py (tuning stream), without synthesis."""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

from axfpga.config import axbench_root
from axfpga.quality import QualitySpec, make_evaluator
from axfpga.quality.attrs import parse_wire_attrs

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "benchmarks" / "axbench"

pytestmark = pytest.mark.skipif(not (axbench_root() / "scripts" / "images" / "peppers.png").exists(),
                                reason="AxBench checkout (AXFPGA_AXBENCH_ROOT) not available")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load(ROOT / "scripts" / "axbench_apps.py", "axbench_apps_ref")
ANN = _load(PLUGIN_DIR / "annotations.py", "axbench_ann_t").ANNOTATIONS

# top module, relaxed wire, relaxed width, data inputs, clocked
DESIGNS = {
    "brent_kung_16b": ("UBBKA_15_0_15_0", "S", 17, [("X", 16), ("Y", 16)], False),
    "brent_kung_32b": ("UBBKA_31_0_31_0", "S", 33, [("X", 32), ("Y", 32)], False),
    "kogge_stone_16b": ("UBKSA_15_0_15_0", "S", 17, [("X", 16), ("Y", 16)], False),
    "kogge_stone_32b": ("UBKSA_31_0_31_0", "S", 33, [("X", 32), ("Y", 32)], False),
    "multiplier_16b": ("Multiplier_15_0_1000", "P", 32, [("IN1", 16), ("IN2", 16)], True),
    "multiplier_32b": ("Multiplier_31_0_3000", "P", 64, [("IN1", 32), ("IN2", 32)], True),
    "fir_filter": ("filterfir", "dataout", 10, [("x", 8)], True),
    "sobel_filter": ("sobel", "out", 8, [(f"p{i}", 8) for i in (0, 1, 2, 3, 5, 6, 7, 8)], True),
    "kinematics_fwd": ("kinematics", "x", 32, [("theta1", 32), ("theta2", 32)], True),
    "kinematics_inv": ("inv_kin", "theta1", 32, [("x", 32), ("y", 32)], True),
    "kmeans_clustering": ("sub_8b", "S", 9, [("A", 8), ("B", 8)], False),
    "neural_network": ("perceptron_fixed", "synapse_out", 16, [("synapse_in", 128), ("weight_in", 128)], True),
    "sobel_npu": ("perceptron_sobel", "synapse_out", 16,
                  [("synapse_in", 160), ("weight_in", 1280), ("hidden_weight", 144)], True),
}


def _evaluator(bid):
    top, wire, width, inputs, clocked = DESIGNS[bid]
    a = ANN[bid]
    raw = {"axc_format": a["format"], "axc_quality": a["quality"], "axc_plugin": a["plugin"], "axc_seed": a["seed"]}
    wq = parse_wire_attrs(raw, wire, width)
    ev = make_evaluator(wq, design_id=bid, top=top, inputs=inputs, outputs=[(wire, width)], has_clock=clocked,
                        base_dir=ROOT, search_dirs=[PLUGIN_DIR])
    return ev, wq


def _streams(bid, vectors, width):
    """Deterministic exact stream and a perturbed copy (low 2 bits flipped every 3rd cycle)."""
    wire = DESIGNS[bid][1]
    rnd = random.Random(5)
    exact = [{wire: rnd.getrandbits(width)} for _ in vectors]
    approx = [dict(r) for r in exact]
    for i in range(0, len(approx), 3):
        approx[i][wire] ^= 3
    return exact, approx


@pytest.mark.parametrize("bid", [b for b in DESIGNS if b != "kmeans_clustering"])
def test_tuning_stream_and_metrics_equal_legacy(bid):
    ev, wq = _evaluator(bid)
    top, wire, width, inputs, clocked = DESIGNS[bid]
    vecs, note = ev.build_stimulus(bid)
    ref_vecs, ref_ctx = ab.build_stimulus(bid)
    assert vecs == ref_vecs and note["note"] == ref_ctx["note"]
    skip = 16 if clocked else 0
    exact, approx = _streams(bid, vecs, width)
    errs = ev.error_stats(bid, exact, approx, wire, skip)
    ref = ab.error_stats(bid, exact, approx, wire, skip)
    for k in ("wce", "med", "mred", "er", "samples"):
        assert errs[k] == ref[k], k
    app = ev.app_metrics(bid, vecs, exact, approx, relax_wire=wire, skip=skip)
    rapp = ab.app_metrics(bid, vecs, exact, approx, wire, skip)
    assert app == rapp          # name, value, unit, extra, threshold text and verdict
    # failure messages equal the legacy assess() lines
    legacy_q = _legacy_fails(bid, ref, rapp)
    fails, _ = ev.quality.evaluate(errs, None, app)
    assert fails == legacy_q


def _legacy_fails(bid, errs, app):
    q = QualitySpec.parse(ANN[bid]["quality"])
    bound = q.wce_bound
    med = q.get("med").threshold
    mred = q.get("mred")
    out = []
    if errs["wce"] > bound:
        out.append(f"WCE {errs['wce']:g} > {bound:g}")
    if errs["med"] > med:
        out.append(f"MED {errs['med']:.3f} > {med:g}")
    if mred is not None and errs["mred"] > mred.threshold:
        out.append(f"MRED {errs['mred'] * 100:.4f}% > {mred.threshold * 100:g}%")
    if app["pass"] is False:
        v = app["value"]
        out.append(f"{app['name']} {v if not isinstance(v, float) else format(v, '.3f')} {app['unit']} fails {app['threshold']}")
    return out


def test_kmeans_metric_and_power_stimulus_equal_legacy():
    bid = "kmeans_clustering"
    ev, _ = _evaluator(bid)
    vecs, note = ev.build_stimulus(bid)
    ref_vecs, ref_ctx = ab.build_stimulus(bid)
    assert vecs == ref_vecs and note["note"] == ref_ctx["note"]
    assert ev.power_vectors(bid, vecs) == ab.power_vectors(bid, ref_vecs)
    exact, approx = _streams(bid, vecs, 9)
    app = ev.app_metrics(bid, vecs, exact, approx, relax_wire="S", skip=0)
    assert app == ab.app_metrics(bid, ref_vecs, exact, approx, "S", 0)


@pytest.mark.parametrize("bid", ["brent_kung_16b", "fir_filter", "neural_network", "kinematics_fwd"])
def test_heldout_stream_differs_is_deterministic(bid):
    ev, _ = _evaluator(bid)
    t, _ = ev.build_stimulus(bid)
    h1, n1 = ev.build_heldout(bid)
    h2, _ = ev.build_heldout(bid)
    assert h1 == h2 and h1 != t and len(h1) == len(t)


def test_heldout_images_exist_for_image_plugins():
    from importlib import import_module  # noqa: F401
    for bid in ("sobel_filter", "sobel_npu"):
        ev, _ = _evaluator(bid)
        t, _ = ev.build_stimulus(bid)
        h, note = ev.build_heldout(bid)
        assert h != t and "baboon" in note["note"]


def test_annotations_cover_every_suite_id():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import run_pyals_yosys_suite as suite
    finally:
        sys.path.pop(0)
    assert {b["id"] for b in suite.define_benchmark_suite()} == set(ANN)


@pytest.mark.parametrize("bid", ["multiplier_16b", "multiplier_32b", "brent_kung_16b"])
def test_arith_stream_independent_of_port_order(bid):
    """Yosys lists the multiplier ports as IN2, IN1; the stream must still match the legacy IN1-first draw."""
    top, wire, width, inputs, clocked = DESIGNS[bid]
    ev, _ = _evaluator(bid)
    ev.inputs = list(reversed(inputs))
    if getattr(ev, "ctx", None) is not None and hasattr(ev.ctx, "inputs"):
        ev.ctx.inputs = list(reversed(inputs))
    vecs, _ = ev.build_stimulus(bid)
    assert vecs == ab.build_stimulus(bid)[0]
