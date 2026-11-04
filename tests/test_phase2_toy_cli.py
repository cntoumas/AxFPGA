"""A design annotated with axc_* attributes only (no Python) runs end to end through the axfpga CLI."""

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parent.parent


def test_toy_add_attributes_only(tmp_path):
    from axfpga import flow
    out = tmp_path / "toy"
    flow.main(["-i", str(ROOT / "benchmarks" / "toy" / "toy_add.v"), "-o", str(out)])
    data = json.loads((out / "result.json").read_text())
    jsonschema.validate(data, json.loads((ROOT / "docs" / "schema" / "result.schema.json").read_text()))
    r = data[0]
    assert [q["metric"] for q in r["quality"]] == ["wce", "med", "psnr"]       # every attribute constraint recorded
    assert not r["fell_back"] and r["errors"]["wce"] <= 15
    h = r["heldout"]                                                            # independent stream, derived seed
    assert h and h["samples"] > 0 and "held-out" in h["note"]
    assert set(h["errors"]) >= {"wce", "med", "mred_pct", "er"}
    assert all(q["pass"] is not None for q in h["quality"])


def test_ports_resolved_after_yosys_read():
    from axfpga.pipeline import _with_meta_ports
    ports = {"inputs": [("a", 4), ("ck", 1), ("rn", 1)], "outputs": [("y", 4)], "clk": None, "rst": None,
             "rst_active_low": False, "all_inputs": [("a", 4), ("ck", 1), ("rn", 1)]}
    assert _with_meta_ports(ports, {}, None, None, None) is ports
    p = _with_meta_ports(ports, {"clock": "ck", "reset": "rn", "reset_active_low": True}, None, None, None)
    assert (p["clk"], p["rst"], p["rst_active_low"], p["inputs"]) == ("ck", "rn", True, [("a", 4)])
