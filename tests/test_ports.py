"""axfpga.ports.resolve_clock_reset precedence."""

import logging

import pytest

from axfpga.ports import resolve_clock_reset


def ins(*names):
    return [{"name": n, "width": 1} for n in names]


def test_guess_legacy_names(caplog):
    with caplog.at_level(logging.WARNING):
        r = resolve_clock_reset(ins("a", "clk", "rst_n"))
    assert (r["clk"], r["rst"], r["rst_active_low"], r["source"]) == ("clk", "rst_n", True, "guess")
    assert "guessed" in caplog.text


def test_guess_active_high_and_extended_names():
    r = resolve_clock_reset(ins("clk_i", "rst"))
    assert (r["clk"], r["rst"], r["rst_active_low"]) == ("clk_i", "rst", False)
    assert resolve_clock_reset(ins("RSTN"))["rst_active_low"] is True


def test_no_clock_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        r = resolve_clock_reset(ins("a", "b"))
    assert r["clk"] is None and r["rst"] is None and r["source"] == "none"
    assert r["rst_active_low"] is True
    assert not caplog.records


def test_attribute_beats_argument_beats_guess(caplog):
    inputs = ins("clk", "ck2", "rst", "my_rst", "arg_rst")
    meta = {"clock": "ck2", "reset": "my_rst", "reset_active_low": True}
    with caplog.at_level(logging.WARNING):
        r = resolve_clock_reset(inputs, meta, clock="clk", reset="arg_rst")
    assert (r["clk"], r["rst"], r["rst_active_low"], r["source"]) == ("ck2", "my_rst", True, "attribute")
    assert not caplog.records
    r = resolve_clock_reset(inputs, {}, clock="ck2", reset="arg_rst", reset_active_low=True)
    assert (r["clk"], r["rst"], r["rst_active_low"], r["source"]) == ("ck2", "arg_rst", True, "argument")


def test_attribute_reset_default_high():
    r = resolve_clock_reset(ins("x", "go"), {"reset": "go"})
    assert r["rst"] == "go" and r["rst_active_low"] is False


def test_attribute_naming_missing_port():
    with pytest.raises(ValueError, match="not an input port"):
        resolve_clock_reset(ins("a"), {"clock": "nope"})
