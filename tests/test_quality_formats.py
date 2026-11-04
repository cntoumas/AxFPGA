import pytest

from axfpga.quality import NumberFormat, QualityError


@pytest.mark.parametrize("text,kind,width,frac", [
    ("u", "u", None, 0), ("u8", "u", 8, 0), ("s32", "s", 32, 0), ("s9", "s", 9, 0),
    ("sm16q9", "sm", 16, 9), ("SM32Q15", "sm", 32, 15), (" sm ", "sm", None, 0), ("sq4", "s", None, 4),
])
def test_parse(text, kind, width, frac):
    f = NumberFormat.parse(text)
    assert (f.kind, f.width, f.frac) == (kind, width, frac)


@pytest.mark.parametrize("bad", ["", "x8", "u8q", "s-3", "sm16q", "u0", "8", "smm8", "u 8"])
def test_parse_errors(bad):
    with pytest.raises(QualityError, match="axc_format of 'y': cannot parse"):
        NumberFormat.parse(bad, "y")


def test_parse_frac_exceeds_width():
    with pytest.raises(QualityError, match=r"axc_format of 'y': fractional bits 9 exceed the width 8"):
        NumberFormat.parse("s8q9", "y")


def test_bind():
    assert NumberFormat.parse("u").bind(17) == NumberFormat("u", 17, 0)
    assert NumberFormat.parse("sm16q9").bind(16).width == 16
    with pytest.raises(QualityError, match="width 16 exceeds the wire width 8"):
        NumberFormat.parse("s16").bind(8, "y")
    with pytest.raises(QualityError, match="fractional bits 12 exceed the width 8"):
        NumberFormat("s", None, 12).bind(8, "y")


def test_str_roundtrip():
    for t in ("u8", "s32", "sm16q9", "sm32q15"):
        assert str(NumberFormat.parse(t)) == t
    assert str(NumberFormat.parse("U")) == "u"


def test_unsigned_unmasked():
    f = NumberFormat.parse("u8").bind(8)
    assert f.decode(0x1FF) == 0x1FF           # legacy: width ignored
    assert f.decode(255) == 255 and f.peak == 255.0


def test_twos_complement():
    f = NumberFormat.parse("s9").bind(9)
    assert [f.decode(v) for v in (0, 1, 255, 256, 511, 512, 0x3FF)] == [0, 1, 255, -256, -1, 0, -1]
    assert f.peak == 255.0
    assert NumberFormat.parse("s1").bind(1).decode(1) == -1


def test_sign_magnitude():
    f = NumberFormat.parse("sm16q9").bind(16)
    assert f.decode(0) == 0
    assert f.decode(0x8000) == 0                      # negative zero
    assert f.decode(0x8001) == -1
    assert f.decode(0x7FFF) == 32767
    assert f.decode(0xFFFF) == -32767
    assert f.decode(0x1_0001) == 1                    # bits above the width ignored
    assert f.decode_real(0x8200) == -1.0              # -512 / 2**9
    assert f.peak == 32767.0


def test_decode_matches_legacy():
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    ab = pytest.importorskip("axbench_apps")
    import random
    rnd = random.Random(5)
    for bid, kind, w, frac in [("multiplier_16b", "s", 32, 0), ("kmeans_clustering", "s", 9, 0), ("neural_network", "sm", 16, 9),
                               ("kinematics_fwd", "sm", 32, 15), ("brent_kung_16b", "u", 17, 0)]:
        f = NumberFormat(kind, w, frac)
        for _ in range(500):
            v = rnd.getrandbits(w + 2)
            assert f.decode(v) == ab.decode(bid, v)
            if kind == "sm":
                assert f.decode_real(v) == ab.sm_decode(v, frac, w)


def test_encode_sm_matches_legacy():
    import sys, pathlib, random
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    ab = pytest.importorskip("axbench_apps")
    rnd = random.Random(1)
    for frac, w in ((9, 16), (15, 32)):
        f = NumberFormat("sm", w, frac)
        for x in [0.0, -0.0, 1e-9, -1e-9, 1e9, -1e9] + [rnd.uniform(-70, 70) for _ in range(300)]:
            assert f.encode(x) == ab.sm_encode(x, frac, w)


def test_encode_sm_edges():
    f = NumberFormat("sm", 8, 0)
    assert f.encode(-1e-9) == 0                       # rounds to zero magnitude: no sign bit
    assert f.encode(1000) == 127
    assert f.encode(-1000) == 0x80 | 127
    assert f.decode(f.encode(-5)) == -5


def test_encode_u_s():
    u = NumberFormat("u", 8, 0)
    assert (u.encode(-3), u.encode(300), u.encode(7.6)) == (0, 255, 8)
    s = NumberFormat("s", 8, 2)
    assert s.encode(-1.0) == 0xFC and s.decode(s.encode(-1.0)) == -4
    assert s.encode(1000) == 127 and s.encode(-1000) == 0x80
    assert s.decode_real(s.encode(1.25)) == 1.25


def test_unbound_errors():
    with pytest.raises(QualityError, match="not bound"):
        NumberFormat.parse("s").decode(1)
    with pytest.raises(QualityError, match="not bound"):
        NumberFormat.parse("sm").peak
