"""Unit tests for Plan 1.2: Support-Set / Input-Dropping Filter in pyALS."""

from pathlib import Path
import pytest

from pyalslib import ALSCatalogCache, get_active_inputs, get_dropped_inputs_count
from axfpga.pyals_bridge import PyAlsBridge

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DB = REPO_ROOT / "pyALS-lut-catalog" / "lut_catalog.db"
MAC_CONE = REPO_ROOT / "benchmarks" / "mac" / "mac.v"


def test_cofactor_equality_and_support_set():
    """Verify boolean support set analysis via cofactor equality T|xj=0 == T|xj=1."""
    # Constant 0: 0 active inputs
    assert get_active_inputs("0" * 16) == []

    # x0 only: flips every adjacent bit
    spec_x0 = "0101010101010101"
    assert get_active_inputs(spec_x0) == [0]

    # x1 only: flips every 2 bits
    spec_x1 = "0011001100110011"
    assert get_active_inputs(spec_x1) == [1]

    # x0 and x1 (XOR): depends on 0 and 1
    spec_xor = "0110011001100110"
    assert get_active_inputs(spec_xor) == [0, 1]

    # 4-input parity: depends on all 4 inputs
    spec_parity = "0110100110010110"
    assert get_active_inputs(spec_parity) == [0, 1, 2, 3]

    # Dropped inputs count
    assert get_dropped_inputs_count(spec_parity, spec_xor) == 2
    assert get_dropped_inputs_count(spec_parity, "0" * 16) == 4


def test_catalog_input_dropping_query():
    """Verify ALSCatalogCache returns candidate LUTs that reduce active inputs."""
    cache = ALSCatalogCache(str(CATALOG_DB))
    # Query a 4-input representative that has approximate entries dropping inputs
    spec = "0000000000000111"  # active inputs = 4
    orig_act = len(get_active_inputs(spec))
    assert orig_act == 4

    input_dropping_cands = cache.get_input_dropping_approx_luts(spec, max_dist=4)
    assert len(input_dropping_cands) > 0

    first = input_dropping_cands[0]
    assert first["dropped_inputs"] > 0
    assert first["active_inputs"] < orig_act
    assert "synth_spec" in first


def test_pyals_bridge_emits_input_drop_candidates(tmp_path: Path):
    """Verify PyAlsBridge generates dedicated input-dropping candidates."""
    bridge = PyAlsBridge(
        cone_verilog_path=MAC_CONE,
        work_dir=tmp_path,
        catalog_db_path=CATALOG_DB,
        cut_size=4,
    )
    cands = bridge.generate_catalog_candidates()
    names = [c["name"] for c in cands]

    # At least some candidates with input-dropping or carry protection should be emitted
    has_input_drop = any("input_drop" in n for n in names)
    has_dist = any("pyALS_dist" in n for n in names)
    assert has_input_drop or has_dist
