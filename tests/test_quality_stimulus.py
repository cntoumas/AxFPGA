import sys
import pathlib

import pytest

from axfpga.quality import (QualityError, derive_seed, load_hex, power_cap, random_vectors, resolve_path,
                            split_file_vectors)

INPUTS = [("a", 8), ("b", 16), ("en", 1), ("clear", 1), ("x", 1)]


def test_derive_seed():
    assert derive_seed(2026, "tuning") == 2026 and derive_seed(1, "tuning") == 1
    h = derive_seed(2026, "heldout")
    assert h == derive_seed(2026, "heldout") and h != 2026 and 0 <= h <= 0x7FFFFFFF
    assert derive_seed(1, "heldout") != derive_seed(2, "heldout")
    with pytest.raises(QualityError):
        derive_seed(1, "bogus")


def test_random_deterministic_and_streams_differ():
    t1, n1 = random_vectors(INPUTS, 200, 7, "tuning")
    t2, _ = random_vectors(INPUTS, 200, 7, "tuning")
    h1, nh = random_vectors(INPUTS, 200, 7, "heldout")
    h2, _ = random_vectors(INPUTS, 200, 7, "heldout")
    assert t1 == t2 and h1 == h2 and t1 != h1
    assert n1["note"] == "seeded random stimulus (200 vectors, seed 7)"
    assert "held-out" in nh["note"]
    # disjoint: no identical 8+16 bit data pair shared in bulk
    ts = {(v["a"], v["b"]) for v in t1}
    hs = {(v["a"], v["b"]) for v in h1}
    assert len(ts & hs) <= 2


def test_random_ranges_and_bias():
    v, _ = random_vectors(INPUTS, 4000, 1, "tuning")
    assert all(0 <= r["a"] < 256 and 0 <= r["b"] < 65536 and r["en"] in (0, 1) for r in v)
    assert sum(r["en"] for r in v) / 4000 > 0.85
    assert sum(r["clear"] for r in v) / 4000 < 0.1
    assert 0.4 < sum(r["x"] for r in v) / 4000 < 0.6


def test_tuning_equals_legacy_generic_evaluator():
    pl = pytest.importorskip("axfpga.pipeline")
    for seed in (1, 2026):
        old, ctx = pl.GenericEvaluator(INPUTS, 8, n_vectors=300, seed=seed).build_stimulus("x")
        new, nctx = random_vectors(INPUTS, 300, seed, "tuning")
        assert old == new and ctx["note"] == nctx["note"]


def test_load_hex(tmp_path):
    p = tmp_path / "s.hex"
    p.write_text("# header\n01 ffff\n\n  1FF 0010 // tail\nAB 0\n")
    v = load_hex(p, [("a", 8), ("b", 16)])
    assert v == [{"a": 1, "b": 0xFFFF}, {"a": 0xFF, "b": 0x10}, {"a": 0xAB, "b": 0}]    # masked to port width


def test_load_hex_errors(tmp_path):
    p = tmp_path / "s.hex"
    p.write_text("01 02\n03\n")
    with pytest.raises(QualityError, match="line 2 has 1 words, the design has 2 data inputs"):
        load_hex(p, [("a", 8), ("b", 8)])
    p.write_text("zz 01\n")
    with pytest.raises(QualityError, match="line 1: 'zz' is not a hex word"):
        load_hex(p, [("a", 8), ("b", 8)])


def test_split_file_vectors():
    vs = [{"a": i} for i in range(129)]
    t, h = split_file_vectors(vs)
    assert t + h == vs and [v["a"] for v in t] == list(range(65)) and len(h) == 64
    t, h = split_file_vectors(vs[:128])
    assert len(t) == len(h) == 64
    with pytest.raises(QualityError, match="file has 127 vectors; at least 128 are needed"):
        split_file_vectors(vs[:127])


def test_power_cap():
    vs = [{"a": i} for i in range(5000)]
    c = power_cap(vs)
    assert len(c) == 2000 and c[0] == vs[0] and c[1] == vs[2] and c[-1] == vs[int(1999 * 2.5)]
    assert power_cap(vs[:2000]) == vs[:2000] and power_cap(vs[:10]) == vs[:10]
    assert len(power_cap(vs, 7)) == 7


def test_power_cap_matches_legacy():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    ab = pytest.importorskip("axbench_apps")
    vs = [{"a": i} for i in range(3000)]
    assert power_cap(vs) == ab.power_vectors("multiplier_16b", vs)


def test_resolve_path(tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    d1.mkdir(); d2.mkdir()
    (d2 / "f.hex").write_text("x")
    assert resolve_path("f.hex", d1, [d2]) == d2 / "f.hex"
    (d1 / "f.hex").write_text("y")
    assert resolve_path("f.hex", d1, [d2]) == d1 / "f.hex"           # base_dir first
    assert resolve_path(str(d2 / "f.hex"), d1) == d2 / "f.hex"
    with pytest.raises(QualityError, match=r"file 'nope.hex' not found \(looked in: .*d1.*d2"):
        resolve_path("nope.hex", d1, [d2])
