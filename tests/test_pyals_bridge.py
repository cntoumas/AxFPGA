"""Unit tests for pyALS AIG-Rewriting Bridge in AxFPGA."""

from pathlib import Path
import pytest
from axfpga.pyals_bridge import PyAlsBridge

REPO_ROOT = Path(__file__).resolve().parent.parent
MAC_CONE = REPO_ROOT / "benchmarks" / "mac" / "mac.v"
CATALOG_DB = REPO_ROOT / "pyALS-lut-catalog" / "lut_catalog.db"


def test_pyals_bridge_init(tmp_path: Path):
    bridge = PyAlsBridge(
        cone_verilog_path=MAC_CONE,
        work_dir=tmp_path,
        catalog_db_path=CATALOG_DB,
    )
    assert bridge.cone_vlog.exists()
    assert bridge.catalog_db.exists()
    assert bridge.cache is not None


def test_pyals_candidate_generation(tmp_path: Path):
    bridge = PyAlsBridge(
        cone_verilog_path=MAC_CONE,
        work_dir=tmp_path,
        catalog_db_path=CATALOG_DB,
        cut_size=4,
    )
    cands = bridge.generate_catalog_candidates()
    assert isinstance(cands, list)
    # At least some catalog or constant pruning candidates should be generated
    assert len(cands) > 0
    first = cands[0]
    assert "name" in first
    assert "strategy" in first
    assert first["strategy"] == "pyals"
    assert Path(first["path"]).exists()
