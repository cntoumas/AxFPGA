import math
import sys
import pathlib
import random

import numpy as np
import pytest

from axfpga.quality import NumberFormat, REGISTRY, compute, decoded, error_stats, psnr_db, sqnr_db


def arr(*x):
    return np.array(x, dtype=np.float64)


def test_registry_flags():
    assert set(REGISTRY) == {"wce", "med", "mred", "er", "rmse", "sqnr", "snr", "psnr", "agreement"}
    assert [REGISTRY[m].higher_is_better for m in ("wce", "med", "mred", "er", "rmse")] == [False] * 5
    assert all(REGISTRY[m].higher_is_better for m in ("sqnr", "snr", "psnr", "agreement"))
    assert {m for m, v in REGISTRY.items() if v.ratio} == {"mred", "er", "agreement"}
    assert REGISTRY["psnr"].unit == "dB" and REGISTRY["agreement"].unit == "%"


E, A = arr(10, 0, -4, 8), arr(7, 1, -4, 12)


def test_error_metrics():
    assert compute("wce", E, A) == 4.0
    assert compute("med", E, A) == (3 + 1 + 0 + 4) / 4
    assert compute("mred", E, A) == pytest.approx((3 / 10 + 0 + 4 / 8) / 3)   # e == 0 excluded
    assert compute("er", E, A) == 0.75
    assert compute("rmse", E, A) == pytest.approx(math.sqrt((9 + 1 + 0 + 16) / 4))


def test_error_metrics_empty_and_zero_ref():
    z = arr()
    for m in ("wce", "med", "mred", "er", "rmse"):
        assert compute(m, z, z) == 0.0
    assert compute("mred", arr(0, 0), arr(1, 2)) == 0.0


def test_sqnr_snr():
    assert compute("sqnr", E, A) == pytest.approx(10 * math.log10((100 + 16 + 64) / 26))
    assert compute("snr", E, A) == compute("sqnr", E, A)
    assert sqnr_db(E, E) == float("inf")
    assert sqnr_db(arr(0, 0), arr(1, 1)) == float("-inf")


def test_psnr():
    assert compute("psnr", E, A, {"peak": 255}) == pytest.approx(10 * math.log10(255 ** 2 / (26 / 4)))
    assert psnr_db(E, E, 255.0) == float("inf")
    with pytest.raises(KeyError):
        compute("psnr", E, A, {})


def test_agreement():
    e, a = arr(-1, 0.2, 0.6, 0.9), arr(-2, 0.7, 0.4, 0.8)
    assert compute("agreement", e, a) == 1.0
    assert compute("agreement", e, a, {"thr": 0.5}) == 0.5
    assert compute("agreement", arr(), arr()) == 0.0


ROWS_E = [{"y": v} for v in (0, 1, 0x1FF, 0x100, 5)]
ROWS_A = [{"y": v} for v in (0, 3, 0x1FE, 0x0FF, 5)]


def test_decoded_and_skip():
    f = NumberFormat("s", 9, 0)
    assert list(decoded(f, ROWS_E, "y", 0, 5)) == [0, 1, -1, -256, 5]
    assert list(decoded(f, ROWS_E, "y", 2, 4)) == [-1, -256]
    assert decoded(f, ROWS_E, "y", 5, 5).size == 0


def test_decoded_wide_words():
    f = NumberFormat("u", 64, 0)
    out = decoded(f, [{"y": (1 << 64) - 1}], "y", 0, 1)
    assert out.dtype == np.float64 and out[0] == float((1 << 64) - 1)


def test_error_stats():
    f = NumberFormat("s", 9, 0)
    s = error_stats(f, ROWS_E, ROWS_A, "y", 0)
    # decoded e = 0,1,-1,-256,5 ; a = 0,3,-2,255,5
    assert s["wce"] == 511.0 and s["samples"] == 5 and s["format"] == "s9"
    assert s["med"] == (0 + 2 + 1 + 511 + 0) / 5
    assert s["er"] == 0.6
    assert s["mred"] == pytest.approx((2 / 1 + 1 / 1 + 511 / 256) / 4)
    s2 = error_stats(f, ROWS_E, ROWS_A[:3], "y", 1)
    assert s2["samples"] == 2 and s2["wce"] == 2.0           # n = min(len), skip applied
    assert "psnr" not in s and "sqnr" not in s
    s3 = error_stats(f, ROWS_E, ROWS_A, "y", 0, extra=("rmse", "psnr", "wce", "bogus"))
    assert set(s3) - set(s) == {"rmse", "psnr"}
    assert s3["psnr"] == compute("psnr", decoded(f, ROWS_E, "y", 0, 5), decoded(f, ROWS_A, "y", 0, 5), {"peak": 255.0})


def test_error_stats_empty():
    f = NumberFormat("u", 8, 0)
    s = error_stats(f, [], [], "y", 0)
    assert (s["wce"], s["med"], s["mred"], s["er"], s["samples"]) == (0.0, 0.0, 0.0, 0.0, 0)


def _legacy():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    return pytest.importorskip("axbench_apps")


@pytest.mark.parametrize("bid,kind,w", [("multiplier_16b", "s", 32), ("kmeans_clustering", "s", 9),
                                       ("neural_network", "sm", 16), ("brent_kung_16b", "u", 17)])
def test_error_stats_matches_legacy(bid, kind, w):
    ab = _legacy()
    rnd = random.Random(3)
    ex = [{"y": rnd.getrandbits(w)} for _ in range(400)]
    ap = [{"y": (r["y"] ^ rnd.getrandbits(3)) & ((1 << w) - 1)} for r in ex]
    old = ab.error_stats(bid, ex, ap, "y", 7)
    new = error_stats(NumberFormat(kind, w, 0), ex, ap, "y", 7)
    for k in ("wce", "med", "mred", "er", "samples"):
        assert new[k] == old[k], k


def test_sqnr_psnr_match_legacy():
    ab = _legacy()
    rng = np.random.default_rng(1)
    e = rng.normal(0, 50, 500)
    a = e + rng.normal(0, 2, 500)
    assert sqnr_db(e, a) == ab._sqnr_db(e, a)
    assert psnr_db(e, a, 255.0) == ab._psnr_db(e, a, 255.0)
    assert sqnr_db(np.zeros(3), a[:3]) == ab._sqnr_db(np.zeros(3), a[:3])
