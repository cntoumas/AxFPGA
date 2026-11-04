"""axc_quality grammar: parsing, errors, evaluation order and message text."""

import pytest

from axfpga.quality import QualityError, QualitySpec


def P(t, w="y"):
    return QualitySpec.parse(t, w)


def test_parse_basic_and_percent():
    q = P("wce<=26; med<=13 ; mred<=1%; app>=99")
    assert [(c.metric, c.op, c.threshold) for c in q.constraints] == [
        ("wce", "<=", 26.0), ("med", "<=", 13.0), ("mred", "<=", 0.01), ("app", ">=", 99.0)]
    assert q.wce_bound == 26.0 and q.med_limit == 13.0 and q.mred_limit == 0.01


def test_case_trailing_semicolon_exponent_sign():
    q = P("WCE <= 4.5e2 ;;")
    assert q.wce_bound == 450.0
    assert P("psnr>=-3").get("psnr").threshold == -3.0


def test_options():
    q = P("psnr[peak=255]>=30; agreement[thr=0.5]>=99%")
    assert dict(q.get("psnr").opts) == {"peak": 255.0}
    assert dict(q.get("agreement").opts) == {"thr": 0.5} and q.get("agreement").threshold == pytest.approx(0.99)


@pytest.mark.parametrize("text,msg", [
    ("wce 26", "cannot parse constraint 'wce 26'; expected metric op number, e.g. 'wce<=26'"),
    ("", "cannot parse constraint ''"),
    ("bogus<=3", "unknown metric 'bogus' (known: agreement, app, er, med, mred, psnr, rmse, snr, sqnr, wce)"),
    ("wce>=3", "'wce>=3' has the wrong direction; wce is lower-is-better (use <= or <)"),
    ("psnr<=3", "'psnr<=3' has the wrong direction; psnr is higher-is-better (use >= or >)"),
    ("wce<=3; wce<=4", "duplicate constraint on 'wce'"),
    ("wce<=3%", "'%' is not allowed on 'wce'"),
    ("app>=3%", "'%' is not allowed on 'app'"),
    ("psnr[foo=1]>=3", "unknown option 'foo' for 'psnr'"),
    ("wce[x=1]<=3", "unknown option 'x' for 'wce'"),
])
def test_errors(text, msg):
    with pytest.raises(QualityError, match="axc_quality of 'y': ") as e:
        P(text)
    assert msg in str(e.value)


def test_evaluation_order_and_messages_match_legacy():
    q = P("app>=30; mred<=1%; med<=7.5; wce<=15; psnr>=40")
    errs = {"wce": 20.0, "med": 8.0, "mred": 0.02, "er": 0.5, "psnr": 31.0}
    app = {"name": "SQNR", "value": 12.3456, "unit": "dB"}
    fails, recs = q.evaluate(errs, None, app)
    assert fails == ["WCE 20 > 15", "MED 8.000 > 7.5", "MRED 2.0000% > 1%",
                     "PSNR 31.000 dB fails >= 40.0 dB", "SQNR 12.346 dB fails >= 30.0 dB"]
    assert [r["metric"] for r in recs] == ["wce", "med", "mred", "psnr", "app"] and not any(r["pass"] for r in recs)
    assert app["threshold"] == ">= 30.0 dB" and app["pass"] is False


def test_equal_passes_and_nan_fails():
    q = P("wce<=15; app>=30")
    app = {"name": "A", "value": 30.0, "unit": "dB"}
    assert q.evaluate({"wce": 15.0}, None, app)[0] == []
    app = {"name": "A", "value": float("nan"), "unit": "dB"}
    assert len(q.evaluate({"wce": 15.0}, None, app)[0]) == 1
    assert app["pass"] is False


def test_strict_operators():
    q = P("wce<15")
    assert q.evaluate({"wce": 15.0})[0] == ["WCE 15 >= 15"]
    assert P("psnr>30").evaluate({"wce": 0, "psnr": 30.0})[0] != []


def test_app_threshold_text_for_lower_is_better():
    q = P("wce<=1; app<=0.05")
    app = {"name": "theta1 RMSE", "value": 0.07, "unit": "rad"}
    assert q.evaluate({"wce": 0.0}, None, app)[0] == ["theta1 RMSE 0.070 rad fails <= 0.05 rad"]
    assert app["threshold"] == "<= 0.05 rad"


def test_legacy_constraints():
    q = QualitySpec.legacy(15.0, "WCE", 7.5, 0.01)
    assert [c.metric for c in q.constraints] == ["wce", "med", "mred"]
    assert q.evaluate({"wce": 16.0, "med": 8.0, "mred": 0.5})[0] == ["WCE 16 > 15", "MED 8.000 > 7.5", "MRED 50.0000% > 1%"]
    m = QualitySpec.legacy(2.0, "MED")
    assert m.wce_bound is None and m.evaluate({"med": 3.0})[0] == ["MED 3.000 > 2"]
    assert QualitySpec.legacy(4.0).evaluate({"wce": 4.0})[0] == []


def test_legacy_duck_typed_app_verdict_still_fails():
    q = QualitySpec.legacy(4.0)
    app = {"name": "X", "value": 1.0, "unit": "dB", "threshold": ">= 30.0 dB", "pass": False}
    assert q.evaluate({"wce": 0.0}, None, app)[0] == ["X 1.000 dB fails >= 30.0 dB"]


def test_with_wce():
    q = P("wce<=26; med<=13").with_wce(10)
    assert q.wce_bound == 10.0 and q.med_limit == 13.0


def test_failing_builtin_higher_is_better_reported_once():
    from axfpga.quality.spec import QualitySpec
    app = {"name": "PSNR", "value": 20.0, "unit": "dB", "threshold": ">= 30.0 dB", "pass": False}
    fails, _ = QualitySpec.parse("wce<=26;psnr>=30").evaluate({"wce": 1}, {"psnr": 20}, app)
    assert len(fails) == 1
