"""Certification tiers and the certified-only headline tables."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

from axfpga.certification import Certification, certification_record, certify, result_tier

ROOT = Path(__file__).resolve().parent.parent
P, B = "PROVEN", "BOUNDED(16)"


@pytest.mark.parametrize("statuses,tier", [
    ([P, P], Certification.CERTIFIED),
    ([P, B], Certification.BOUNDED),
    ([B, B], Certification.BOUNDED),
    ([P, "TIMEOUT"], Certification.UNCERTIFIED),
    ([P, "INCONCLUSIVE"], Certification.UNCERTIFIED),
    ([P, "ERROR"], Certification.UNCERTIFIED),
    ([P, "N/A (design-level PPG pruning; errors measured on application stream)"], Certification.UNCERTIFIED),
    ([P, "FALSIFIED"], Certification.UNCERTIFIED),
    ([], Certification.UNCERTIFIED),
    ([P, "garbage"], Certification.UNCERTIFIED),
])
def test_tiers(statuses, tier):
    assert certify(statuses) is tier


def test_exact_design_is_certified():
    assert certify(["N/A (exact design kept)"] * 2, exact_design=True) is Certification.CERTIFIED


def test_record_has_reason_when_not_certified():
    rec = certification_record(P, "TIMEOUT")
    assert rec["tier"] == "uncertified" and "wce_bound=TIMEOUT" in rec["reason"]
    assert rec["proofs"] == {"control_equivalence": "PROVEN", "wce_bound": "TIMEOUT"}
    assert certification_record(P, P) == {"tier": "certified", "proofs": {"control_equivalence": P, "wce_bound": P}, "reason": ""}
    assert certification_record("N/A (exact design kept)", "N/A (exact design kept)", exact_design=True)["tier"] == "certified"


def test_legacy_result_without_field_is_derived():
    r = {"formal": {"control_equivalence": P, "wce_bound": "N/A (x)"}, "fell_back": False}
    assert result_tier(r) is Certification.UNCERTIFIED
    r = {"formal": {"control_equivalence": P, "wce_bound": P}}
    assert result_tier(r) is Certification.CERTIFIED


def test_export_headline_tables_only_certified(tmp_path):
    spec = importlib.util.spec_from_file_location("suite", ROOT / "scripts" / "run_pyals_yosys_suite.py")
    suite = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(suite)
    finally:
        sys.path.pop(0)
    base = json.loads((ROOT / "tests" / "data" / "results_anchor.json").read_text())
    good = next(r for r in base if r["id"] == "brent_kung_16b")
    bad = json.loads(json.dumps(good))
    bad.update(id="brent_kung_32b", name="BAD DESIGN")
    bad["formal"]["wce_bound"] = "TIMEOUT"
    bad["certification"] = certification_record(bad["formal"]["control_equivalence"], "TIMEOUT")
    suite.export_results([good, bad], tmp_path)
    md = (tmp_path / "report.md").read_text()
    head, _, tail = md.partition("## Not certified")
    assert good["name"] in head and "BAD DESIGN" not in head
    assert "BAD DESIGN" in tail and "wce_bound=TIMEOUT" in tail
    rows = list(csv.DictReader((tmp_path / "results.csv").open()))
    assert {r["id"]: r["certification"] for r in rows} == {"brent_kung_16b": "certified", "brent_kung_32b": "uncertified"}
    assert len(json.loads((tmp_path / "results.json").read_text())) == 2
