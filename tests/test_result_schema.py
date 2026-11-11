"""docs/schema/result.schema.json matches the real result record."""

import copy
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs" / "schema" / "result.schema.json").read_text())
ANCHOR = json.loads((ROOT / "tests" / "data" / "results_anchor.json").read_text())


def _with_cert(r):
    from axfpga.certification import certification_record
    r = copy.deepcopy(r)
    f = r["formal"]
    r["certification"] = certification_record(f["control_equivalence"], f["wce_bound"], exact_design=r["fell_back"])
    return r


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_phase0_anchor_records_validate_with_certification():
    jsonschema.validate([_with_cert(r) for r in ANCHOR], SCHEMA, cls=jsonschema.Draft202012Validator)


def test_record_without_certification_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([copy.deepcopy(ANCHOR[0])], SCHEMA, cls=jsonschema.Draft202012Validator)


def test_bad_tier_and_proof_status_rejected():
    r = _with_cert(ANCHOR[0])
    r["certification"]["tier"] = "gold"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([r], SCHEMA, cls=jsonschema.Draft202012Validator)
    r = _with_cert(ANCHOR[0])
    r["formal"]["wce_bound"] = "MAYBE"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate([r], SCHEMA, cls=jsonschema.Draft202012Validator)
