"""_heldout caches the held-out stimulus and the exact outputs across candidates (used by the PPG acceptance gate)."""
from pathlib import Path

from axfpga import pipeline
from axfpga.quality.spec import QualitySpec


class FakeEval:
    def __init__(self):
        self.built = 0

    def build_heldout(self, bench_id):
        self.built += 1
        return [{"a": 1}, {"a": 2}], {"note": "held"}

    def error_stats(self, bench_id, exact, approx, wire, skip):
        wce = max(abs(e[wire] - a[wire]) for e, a in zip(exact, approx))
        return {"wce": float(wce), "med": float(wce), "mred": 0.0, "er": 0.0}

    def app_metrics_for(self, stream, vectors, exact, approx, skip):
        return {"name": "app", "value": 0.0, "unit": "", "threshold": "", "pass": True}


def test_heldout_cache_and_violations(monkeypatch, tmp_path):
    sims = []

    def fake_sim(files, vectors, tag, **kw):
        sims.append(tag)
        off = 0 if tag == "heldout_exact" else (5 if "bad" in tag else 0)
        return {"outputs": [{"y": v["a"] + off} for v in vectors]}

    monkeypatch.setattr(pipeline, "run_vector_sim", fake_sim)
    monkeypatch.setattr(pipeline, "_normalize_rtl", lambda src, top, out: out)
    ev, cache, q = FakeEval(), {}, QualitySpec.parse("wce<=2")
    common = dict(evaluator=ev, quality=q, bench_id="b", wire="y", skip=0, tuning_vectors=[], exact_out=[], approx_out=[],
                  exact_norm=tmp_path / "e.v", top_mod="t", sim_kw={"work_dir": tmp_path}, cache=cache)
    ok = pipeline._heldout(approx_top=Path("a.v"), tag="heldout_good", **common)
    bad = pipeline._heldout(approx_top=Path("a.v"), tag="heldout_bad", **common)
    assert ok["within_limits"] and not bad["within_limits"]
    assert bad["violations"]
    assert ev.built == 1                        # stimulus built once
    assert sims.count("heldout_exact") == 1     # exact outputs simulated once
