"""Reproducibility manifest (scripts/strip_results.py)."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "strip_results.py"
spec = importlib.util.spec_from_file_location("strip_results", SCRIPT)
strip_results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strip_results)


def _run(root: Path, wall_s: float, mapped: str) -> None:
    (root / "reports").mkdir(parents=True)
    (root / "work" / "b1").mkdir(parents=True)
    rec = {"id": "b1", "wall_s": wall_s, "elapsed_seconds": wall_s * 2, "method": "x",
           "log": f"wrote {root / 'work' / 'b1' / 'a.v'}", "nested": {"run_time": wall_s, "lcs": 12}}
    (root / "reports" / "results.json").write_text(json.dumps([rec]))
    (root / "work" / "b1" / "exact_mapped.v").write_text("module e; endmodule\n")
    (root / "work" / "b1" / "approx_mapped.v").write_text(mapped)


def test_times_and_paths_do_not_change_the_manifest(tmp_path: Path):
    _run(tmp_path / "a", 1.0, "module a; endmodule\n")
    _run(tmp_path / "somewhere_else", 99.0, "module a; endmodule\n")
    ma = strip_results.manifest(tmp_path / "a" / "reports", tmp_path / "a" / "work", [])
    mb = strip_results.manifest(tmp_path / "somewhere_else" / "reports", tmp_path / "somewhere_else" / "work", [])
    assert ma["manifest"] == mb["manifest"]
    assert '"lcs": 12' in ma["stripped"]
    assert "wall_s" not in ma["stripped"] and "run_time" not in ma["stripped"]


def test_a_different_netlist_changes_the_manifest(tmp_path: Path):
    _run(tmp_path / "a", 1.0, "module a; endmodule\n")
    _run(tmp_path / "b", 1.0, "module a; wire w; endmodule\n")
    ma = strip_results.manifest(tmp_path / "a" / "reports", tmp_path / "a" / "work", [])
    mb = strip_results.manifest(tmp_path / "b" / "reports", tmp_path / "b" / "work", [])
    assert ma["manifest"]["results.json"] == mb["manifest"]["results.json"]
    assert ma["manifest"]["netlists"]["b1/approx_mapped.v"] != mb["manifest"]["netlists"]["b1/approx_mapped.v"]


def test_certification_field_is_ignored(tmp_path: Path):
    """Results with the post-Phase-0 certification field strip to the same text as the anchor."""
    base = {"id": "b1", "formal": {"wce_bound": "PROVEN"}}
    with_cert = dict(base, certification={"tier": "certified", "proofs": {}, "reason": ""})
    assert strip_results.strip(with_cert, []) == strip_results.strip(base, [])
