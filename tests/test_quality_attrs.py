"""axc_* attribute grammar: parse_wire_attrs / parse_port_attrs / WireQuality round trip."""

import logging

import pytest

from axfpga.quality.attrs import (WireQuality, has_phase2_attrs, known_attr_names,
                                  parse_port_attrs, parse_wire_attrs)
from axfpga.quality.spec import QualityError


def parse(raw, width=16):
    return parse_wire_attrs(raw, "y", width)


def test_full_example_from_grammar():
    wq = parse({"axc_relax": 1, "axc_format": "sm16q9",
                "axc_quality": "psnr>=30;wce<=26;med<=13", "axc_stimulus": "img.hex"})
    assert str(wq.fmt) == "sm16q9"
    assert wq.stimulus == "img.hex"
    assert wq.wce_bound == 26
    assert [c.metric for c in wq.quality.constraints] == ["psnr", "wce", "med"]
    assert wq.seed == 1 and wq.samples is None


def test_whitespace_case_and_trailing_semicolon():
    wq = parse({"axc_quality": "  WCE <= 5 ;  Med<2.5 ; "})
    assert [(c.metric, c.op, c.threshold) for c in wq.quality.constraints] == [
        ("wce", "<=", 5.0), ("med", "<", 2.5)]


def test_percent_and_ops():
    wq = parse({"axc_quality": "wce<=3; mred<=1%; agreement>99%; app>=99",
                "axc_plugin": "p.py"})
    by = {c.metric: c for c in wq.quality.constraints}
    assert by["mred"].threshold == pytest.approx(0.01)
    assert by["agreement"].op == ">" and by["agreement"].threshold == pytest.approx(0.99)
    assert by["app"].threshold == 99


def test_metric_options():
    wq = parse({"axc_quality": "wce<=1; psnr[peak=255]>=30"})
    assert dict(wq.quality.get("psnr").opts) == {"peak": 255.0}


@pytest.mark.parametrize("text, frag", [
    ("wce 5", "cannot parse constraint"),
    ("wce<=", "cannot parse constraint"),
    ("wce<=abc", "cannot parse constraint"),
    ("<=5", "cannot parse constraint"),
    ("bogus<=5; wce<=1", "unknown metric 'bogus'"),
    ("wce>=5", "wrong direction"),
    ("wce<=5; psnr<=30", "wrong direction"),
    ("wce<=5; wce<=6", "duplicate constraint on 'wce'"),
    ("wce<=5%", "'%' is not allowed on 'wce'"),
    ("wce<=5; psnr[foo=1]>=30", "unknown option 'foo'"),
])
def test_bad_quality_text(text, frag):
    with pytest.raises(QualityError) as e:
        parse({"axc_quality": text, "axc_plugin": "p.py"})
    assert frag in str(e.value)
    assert "axc_quality of 'y'" in str(e.value)


def test_quality_error_is_valueerror():
    assert issubclass(QualityError, ValueError)


def test_needs_wce_or_bound():
    with pytest.raises(QualityError, match="needs a wce<= constraint"):
        parse({"axc_quality": "med<=1"})
    wq = parse({"axc_quality": "med<=1", "axc_bound": 7})
    assert wq.wce_bound == 7.0


def test_wce_wins_over_axc_bound_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        wq = parse({"axc_quality": "wce<=5", "axc_bound": 9})
    assert wq.wce_bound == 5.0
    assert "overrides axc_bound" in caplog.text


def test_app_needs_plugin():
    with pytest.raises(QualityError, match="'app' needs an axc_plugin"):
        parse({"axc_quality": "wce<=5; app>=99"})


def test_legacy_only_attrs_are_not_phase2():
    raw = {"axc_relax": 1, "axc_metric": "WCE", "axc_bound": 4, "axc_lsb_width": 8}
    assert not has_phase2_attrs(raw)
    wq = parse(raw)
    assert wq.fmt is None and wq.quality is None and wq.wce_bound == 4.0


def test_bad_axc_bound():
    with pytest.raises(QualityError, match="axc_bound of 'y' is not a number"):
        parse({"axc_bound": "lots"})


@pytest.mark.parametrize("text", ["sm16q9", "u", "u8", "s32", "SM16Q9", " s9 "])
def test_format_ok(text):
    assert parse({"axc_format": text}, width=32).fmt is not None


@pytest.mark.parametrize("text, frag", [
    ("x7", "cannot parse"), ("sm16q", "cannot parse"), ("", "cannot parse"),
    ("u32", "exceeds the wire width"), ("sm8q9", "exceed the width"),
])
def test_format_bad(text, frag):
    with pytest.raises(QualityError) as e:
        parse({"axc_format": text}, width=16)
    assert frag in str(e.value)


def test_format_binds_wire_width():
    assert parse({"axc_format": "s"}, width=12).fmt.width == 12


def test_seed_samples_stimulus_plugin():
    wq = parse({"axc_seed": 2026, "axc_samples": 500, "axc_plugin": "nn.py"})
    assert (wq.seed, wq.samples, wq.plugin) == (2026, 500, "nn.py")
    assert parse({"axc_seed": "7"}).seed == 7
    with pytest.raises(QualityError, match="axc_samples"):
        parse({"axc_samples": 10})
    with pytest.raises(QualityError, match="axc_seed"):
        parse({"axc_seed": "x"})
    with pytest.raises(QualityError, match="axc_stimulus"):
        parse({"axc_stimulus": ""})


def test_reserved_attrs_stored():
    wq = parse({"axc_max_sites": 3, "axc_ops": "Trunc, loa", "axc_horizon": 100,
                "axc_clear_period": 10})
    assert wq.reserved == {"max_sites": 3, "ops": ["trunc", "loa"], "horizon": 100,
                           "clear_period": 10}
    with pytest.raises(QualityError, match="axc_ops"):
        parse({"axc_ops": "trunc,magic"})
    with pytest.raises(QualityError, match="axc_horizon"):
        parse({"axc_horizon": 0})


def test_unknown_attr_warns_with_suggestion(caplog):
    with caplog.at_level(logging.WARNING):
        parse({"axc_qualty": "wce<=1"})
    assert "unknown attribute 'axc_qualty' on 'y'; did you mean 'axc_quality'?" in caplog.text


def test_clock_reset_on_relaxed_wire_rejected():
    with pytest.raises(QualityError, match="input port"):
        parse({"axc_clock": 1})


def test_known_names():
    k = known_attr_names()
    assert {"axc_relax", "axc_clock", "axc_reset", "axc_quality", "axc_horizon"} <= k


def test_round_trip():
    raw = {"axc_format": "sm16q9", "axc_quality": "wce<=26; med<=13; app>=99",
           "axc_plugin": "nn.py", "axc_seed": 2026, "axc_ops": "trunc"}
    wq = parse(raw)
    d = wq.to_dict()
    import json
    d2 = json.loads(json.dumps(d))
    wq2 = WireQuality.from_dict(d2)
    assert wq2.to_dict() == d
    assert wq2.wce_bound == 26.0 and str(wq2.fmt) == "sm16q9"


def test_port_roles():
    assert parse_port_attrs({"axc_clock": 1}, "clk") == {"clock": True, "reset": None}
    assert parse_port_attrs({"axc_reset": 1}, "r")["reset"] == "high"
    assert parse_port_attrs({"axc_reset": "LOW"}, "r")["reset"] == "low"
    assert parse_port_attrs({}, "a") == {"clock": False, "reset": None}
    with pytest.raises(QualityError, match="axc_reset of 'r'"):
        parse_port_attrs({"axc_reset": "sideways"}, "r")
    with pytest.raises(QualityError, match="both"):
        parse_port_attrs({"axc_reset": 1, "axc_clock": 1}, "r")
    with pytest.raises(QualityError, match="axc_relax"):
        parse_port_attrs({"axc_clock": 1, "axc_relax": 1}, "clk")
