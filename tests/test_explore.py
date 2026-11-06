"""Design-space exploration (axfpga.explore): per-site operators, evaluator metrics, search strategies, Pareto."""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from axfpga.explore.evaluate import error_metrics, violations
from axfpga.explore.search import Exploration, hypervolume, pareto
from axfpga.explore.techniques import COMP_SET, CatalogTechnique, SiteOpsTechnique, genome_key

TOY = """module toy(a, b, sa, sb, p, s, sp);
  input [5:0] a;
  input [5:0] b;
  input signed [5:0] sa;
  input signed [5:0] sb;
  output [11:0] p;
  output [6:0] s;
  output signed [11:0] sp;
  assign s = a + b;
  assign p = a * b;
  assign sp = sa * sb;
endmodule
"""

CONE = """module cone1(a, b, y);
  input [3:0] a;
  input [3:0] b;
  output [4:0] y;
  assign y = a + b;
endmodule
"""


def _sgn(x, w):
    return x - (1 << w) if (x >> (w - 1)) & 1 else x


def _ppg_u(a, b, k, n=6):
    return sum(((a >> i) & 1) * ((b >> j) & 1) << (i + j) for i in range(n) for j in range(n) if i + j >= k) & 0xFFF


def _ppg_s(a, b, k, n=6):
    A, B = [(a >> i) & 1 for i in range(n)], [(b >> i) & 1 for i in range(n)]
    t = sum(A[i] * B[j] << (i + j) for i in range(n - 1) for j in range(n - 1) if i + j >= k)
    t += A[n - 1] * B[n - 1] << (2 * n - 2) if 2 * n - 2 >= k else 0
    t -= sum(A[n - 1] * B[j] << (n - 1 + j) for j in range(n - 1) if n - 1 + j >= k)
    t -= sum(A[i] * B[n - 1] << (i + n - 1) for i in range(n - 1) if i + n - 1 >= k)
    return _sgn(t & 0xFFF, 12)


def _apply(tmp_path, genomes):
    src = tmp_path / "toy.v"
    src.write_text(TOY)
    spec = {"cone": str(src), "out_wire": "s", "genomes": genomes, "out_dir": str(tmp_path), "list_sites": True}
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    subprocess.run([sys.executable, "-m", "axfpga.explore.ops", str(tmp_path / "spec.json"), str(tmp_path / "res.json")],
                   check=True, capture_output=True)
    return json.loads((tmp_path / "res.json").read_text())


def _simulate(tmp_path, path):
    tb = tmp_path / "tb.v"
    out = tmp_path / "o.txt"
    tb.write_text(f"""module tb; reg [5:0] a,b; wire [11:0] p; wire [6:0] s; wire signed [11:0] sp; integer i,j,f;
toy u(.a(a),.b(b),.sa(a),.sb(b),.p(p),.s(s),.sp(sp));
initial begin f=$fopen("{out}"); for(i=0;i<64;i=i+1) for(j=0;j<64;j=j+1) begin a=i;b=j; #1 $fdisplay(f,"%0d %0d %0d",p,s,sp); end $fclose(f); $finish; end endmodule""")
    subprocess.run(f"iverilog -g2012 -o {tmp_path}/t.vvp {tb} {path} && vvp {tmp_path}/t.vvp", shell=True, check=True,
                   capture_output=True)
    return [tuple(int(x) for x in ln.split()) for ln in out.read_text().split("\n") if ln.strip()]


def test_sites_and_operators_match_models(tmp_path):
    res = _apply(tmp_path, {})
    ids = {s["id"]: s for s in res["sites"]}
    add = next(i for i in ids if i.startswith("add@"))
    muls = sorted(i for i in ids if i.startswith("mul@"))
    assert "out@s" in ids and "wire@a" in ids and len(muls) == 2
    assert ids[add]["ops"] == ["trunc", "loa", "nibble"] and "ppg" in ids[muls[0]]["ops"]
    genomes = {"loa2": {add: {"op": "loa", "k": 2}}, "nib1": {add: {"op": "nibble", "k": 1}},
               "ppg4": {muls[0]: {"op": "ppg", "k": 4}}, "sppg4": {muls[1]: {"op": "ppg", "k": 4}},
               "wire2": {"wire@a": {"op": "wire", "k": 2, "comp": "mid"}}, "out2": {"out@s": {"op": "out", "k": 2, "comp": "mid"}}}
    res = _apply(tmp_path, genomes)
    assert not res["errors"]

    def model(name, a, b):
        p, s, sp = a * b, a + b, _sgn(a, 6) * _sgn(b, 6)
        if name == "loa2":
            s = (((a >> 2) + (b >> 2) + ((a >> 1) & (b >> 1) & 1)) << 2) | ((a | b) & 3)
        if name == "nib1":
            s = (((a & 15) + (b & 15)) & 15) | (((a >> 4) + (b >> 4)) << 4)
        if name == "ppg4":
            p = _ppg_u(a, b, 4)
        if name == "sppg4":
            sp = _ppg_s(a, b, 4)
        if name == "wire2":
            aa = (a & ~3) | 2
            p, s = aa * b, aa + b
        if name == "out2":
            s = (s & ~3) | 2
        return p & 0xFFF, s & 0x7F, sp

    for name, path in res["written"].items():
        got = _simulate(tmp_path, path)
        bad = [(i, g) for i, g in enumerate(got) if g != model(name, *divmod(i, 64))]
        assert not bad, f"{name}: {len(bad)} mismatches, first {bad[0]}"


def test_error_metrics_and_violations():
    e = np.array([10.0, 20.0, 0.0, 5.0])
    a = np.array([10.0, 18.0, 1.0, 5.0])
    m = error_metrics(e, a, 8)
    assert m["wce"] == 2 and m["er"] == 0.5 and m["med"] == 0.75 and m["bias"] == pytest.approx(-0.25)
    assert m["mred"] == pytest.approx((0 + 0.1 + 0) / 3) and m["wce_pct"] == pytest.approx(200 / 255)
    msgs, v = violations(m, {"wce": 1.0, "med": 1.0})
    assert msgs == ["wce 2 > 1"] and v == pytest.approx(1.0)


def test_pareto_and_hypervolume():
    pts = [(10, 5, "a"), (8, 7, "b"), (12, 1, "c"), (9, 9, "d"), (8, 8, "e")]
    assert [t for _, _, t in pareto(pts)] == ["b", "a", "c"]
    assert hypervolume([(5, 0, None)], 10, 1) == pytest.approx(0.5)
    assert hypervolume([], 10, 1) == 0.0


def test_genome_key_ignores_untouched_sites():
    assert genome_key({"x": {"op": "trunc", "k": 0}, "y": {"op": "trunc", "k": 2}}) == genome_key({"y": {"op": "trunc", "k": 2}})


class _FakeEval:
    """Cost falls with depth, error grows with depth; feasible up to error 4."""
    base_cost = 100.0
    limits = {"wce": 4.0}

    def __init__(self):
        self.n = 0

    def evaluate(self, path):
        self.n += 1
        g = json.loads(Path(path).read_text())
        k = sum(v["k"] for v in g.values()) if g and "index" not in g else g.get("index", 0)
        err = float(k)
        feas = err <= 4
        return {"ok": True, "feasible": feas, "errors": {"wce": err}, "violation": 0 if feas else err - 4,
                "violations": [] if feas else ["wce"], "cost": 100 - 5 * k if feas else None, "lcs": None, "luts": None,
                "dsps": 0, "carry4": 0}

    def fitness(self, rec):
        return rec["cost"] if rec["feasible"] else 1000.0


class _JsonTech(SiteOpsTechnique):
    def __init__(self, sites):
        super().__init__("trunc", sites, Path("unused.v"), "m", None)

    def materialize(self, genomes, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for n, g in genomes.items():
            p = out_dir / f"{n}.json"
            p.write_text(json.dumps(g))
            out[n] = p
        return out


SITES = [{"id": f"add@x:{i}", "kind": "add", "ops": ["trunc"], "max_k": 3, "weight": 10 - i, "y_width": 8} for i in range(3)]


def test_exhaustive_when_space_fits_budget(tmp_path):
    t = _JsonTech(SITES[:1])
    assert t.size() == 1 + 3 * len(COMP_SET)
    s = Exploration(t, _FakeEval(), tmp_path, budget_fe=50, jobs=1).run()
    assert s["strategy"] == "exhaustive" and s["evaluations"] == 7
    assert s["best"]["cost"] == 85


@pytest.mark.parametrize("algo", ["EliteSingleGA", "OriginalSA", "random", "greedy"])
def test_search_finds_feasible_optimum_and_is_seeded(tmp_path, algo):
    t = _JsonTech(SITES)
    s1 = Exploration(t, _FakeEval(), tmp_path / "a", algorithm=algo, budget_fe=60, jobs=1, seed=3, sweep=False).run()
    s2 = Exploration(t, _FakeEval(), tmp_path / "b", algorithm=algo, budget_fe=60, jobs=1, seed=3, sweep=False).run()
    assert s1["best"] is not None and s1["best"]["cost"] <= 85
    assert s1["best"]["cost"] == s2["best"]["cost"]
    assert s1["front"] and all(r["feasible"] for r in s1["front"])


def test_catalog_enumerates_every_candidate(tmp_path):
    files = []
    for i in range(3):
        p = tmp_path / f"c{i}.json"
        p.write_text(json.dumps({"index": i}))
        files.append((f"cand{i}", p))
    t = CatalogTechnique("demo", "demo family", files)
    s = Exploration(t, _FakeEval(), tmp_path / "x", budget_fe=1, jobs=1).run()
    assert s["strategy"] == "exhaustive" and s["evaluations"] == 3 and s["best"]["describe"] == "cand2"
