"""Phase 5 operators (axfpga.operators): site enumeration, operand truncation with correction, output truncation,
genome application in a subprocess, greedy neighbours; behaviour checked by iverilog against a Python model."""

import random
import shutil
from pathlib import Path

import pytest

from axfpga.operators import build_menu, neighbours, ranked_sites, run_operators
from axfpga.operators.generator import next_k

needs_tools = pytest.mark.skipif(shutil.which("iverilog") is None or shutil.which("vvp") is None, reason="iverilog not installed")

ADD = """module addc(a, b, y);
  input [15:0] a;
  input [15:0] b;
  output [16:0] y;
  assign y = a + b;
endmodule
"""
MUL = """module mulc(x, y);
  input [15:0] x;
  output [23:0] y;
  assign y = x * 8'd13;
endmodule
"""
SUB = """module subc(a, b, y);
  input [11:0] a;
  input [11:0] b;
  output [11:0] y;
  assign y = a - b;
endmodule
"""


def _cone(tmp_path, src, name):
    p = tmp_path / f"{name}.v"
    p.write_text(src)
    return p


def _run(tmp_path, cone, module, out_wire, genomes, sites=False):
    return run_operators(cone, module, out_wire, genomes, tmp_path / "ops", want_sites=sites)


def _simulate(tmp_path, cand, module, ins, out, out_w, vectors, tag):
    """Output of ``cand`` for every input vector (dict name -> int), by iverilog."""
    decl = "\n".join(f"  reg [{w - 1}:0] {n};" for n, w in ins.items())
    conn = ", ".join(f".{n}({n})" for n in ins) + f", .{out}(o)"
    body = "\n".join("    " + " ".join(f"{n} = {w}'d{v[n]};" for n, w in ins.items()) + f" #1 $display(\"R=%0d\", o);" for v in vectors)
    tb = tmp_path / f"tb_{tag}.v"
    tb.write_text(f"module tb;\n{decl}\n  wire [{out_w - 1}:0] o;\n  {module} dut({conn});\n  initial begin\n{body}\n    $finish;\n  end\nendmodule\n")
    import subprocess
    vvp = tmp_path / f"{tag}.vvp"
    subprocess.run(["iverilog", "-g2012", "-o", str(vvp), str(tb), str(cand)], check=True, capture_output=True)
    res = subprocess.run(["vvp", str(vvp)], check=True, capture_output=True, text=True)
    return [int(x[2:]) for x in res.stdout.split() if x.startswith("R=")]


def test_sites_have_stable_ids_and_caps(tmp_path):
    cone = _cone(tmp_path, ADD, "addc")
    r = _run(tmp_path, cone, "addc", "y", {}, sites=True)
    arith = [s for s in r["sites"] if s["kind"] != "out"]
    assert len(arith) == 1 and arith[0]["kind"] == "add" and arith[0]["id"].startswith("add@")
    assert arith[0]["max_k"] == 15                      # variable 16-bit operands: at most 15 bits
    assert any(s["id"] == "out@y" and s["max_k"] == 16 for s in r["sites"])
    r2 = _run(tmp_path, cone, "addc", "y", {}, sites=True)
    assert [s["id"] for s in r2["sites"]] == [s["id"] for s in r["sites"]]


def test_constant_multiplier_site_truncates_only_the_variable_operand(tmp_path):
    cone = _cone(tmp_path, MUL, "mulc")
    r = _run(tmp_path, cone, "mulc", "y", {}, sites=True)
    mul = next(s for s in r["sites"] if s["kind"] == "mul")
    assert (mul["a"]["const"], mul["b"]["const"]).count(True) == 1
    assert mul["max_k"] == 15


@needs_tools
@pytest.mark.parametrize("comp, expect_c", [("zero", lambda k: 0), ("mid", lambda k: 1 << (k - 1)), ("max", lambda k: (1 << k) - 1)])
def test_add_truncation_matches_model(tmp_path, comp, expect_c):
    cone = _cone(tmp_path, ADD, "addc")
    sid = next(s["id"] for s in _run(tmp_path, cone, "addc", "y", {}, sites=True)["sites"] if s["kind"] == "add")
    k = 4
    r = _run(tmp_path, cone, "addc", "y", {"g": {sid: {"k": k, "comp": comp}}})
    assert not r["errors"]
    rnd = random.Random(1)
    vecs = [{"a": rnd.getrandbits(16), "b": rnd.getrandbits(16)} for _ in range(300)] + [{"a": 65535, "b": 65535}, {"a": 0, "b": 0}]
    got = _simulate(tmp_path, r["written"]["g"], "addc", {"a": 16, "b": 16}, "y", 17, vecs, "add")
    mask = ~((1 << k) - 1)
    want = [(((v["a"] & mask) + (v["b"] & mask)) | expect_c(k)) & 0x1FFFF for v in vecs]
    assert got == want


@needs_tools
def test_mul_by_constant_truncation_matches_model(tmp_path):
    cone = _cone(tmp_path, MUL, "mulc")
    sid = next(s["id"] for s in _run(tmp_path, cone, "mulc", "y", {}, sites=True)["sites"] if s["kind"] == "mul")
    k = 3
    r = _run(tmp_path, cone, "mulc", "y", {"g": {sid: {"k": k, "comp": "mid"}}})
    rnd = random.Random(2)
    vecs = [{"x": rnd.getrandbits(16)} for _ in range(200)]
    got = _simulate(tmp_path, r["written"]["g"], "mulc", {"x": 16}, "y", 24, vecs, "mul")
    want = [(((v["x"] & ~((1 << k) - 1)) * 13) | (1 << (k - 1))) & 0xFFFFFF for v in vecs]
    assert got == want


@needs_tools
def test_output_truncation_matches_model(tmp_path):
    cone = _cone(tmp_path, SUB, "subc")
    r = _run(tmp_path, cone, "subc", "y", {"g": {"out@y": {"k": 3, "comp": "mid"}}})
    rnd = random.Random(3)
    vecs = [{"a": rnd.getrandbits(12), "b": rnd.getrandbits(12)} for _ in range(200)]
    got = _simulate(tmp_path, r["written"]["g"], "subc", {"a": 12, "b": 12}, "y", 12, vecs, "sub")
    want = [(((v["a"] - v["b"]) & 0xFFF) & ~7) | 4 for v in vecs]
    assert got == want


@needs_tools
def test_stacked_genome_applies_every_gene(tmp_path):
    src = """module two(a, b, c, y);
  input [11:0] a;
  input [11:0] b;
  input [11:0] c;
  output [12:0] y;
  wire [12:0] s = a + b;
  assign y = s + c;
endmodule
"""
    cone = _cone(tmp_path, src, "two")
    sites = [s for s in _run(tmp_path, cone, "two", "y", {}, sites=True)["sites"] if s["kind"] == "add"]
    assert len(sites) == 2
    genome = {s["id"]: {"k": 2, "comp": "zero"} for s in sites}
    r = _run(tmp_path, cone, "two", "y", {"g": genome})
    rnd = random.Random(4)
    vecs = [{"a": rnd.getrandbits(12), "b": rnd.getrandbits(12), "c": rnd.getrandbits(12)} for _ in range(200)]
    got = _simulate(tmp_path, r["written"]["g"], "two", {"a": 12, "b": 12, "c": 12}, "y", 13, vecs, "two")
    m = ~3
    want = [((((v["a"] & m) + (v["b"] & m)) & 0x1FFF) & m) + (v["c"] & m) & 0x1FFF for v in vecs]
    assert got == want


def test_unknown_site_is_reported_not_raised(tmp_path):
    cone = _cone(tmp_path, ADD, "addc")
    r = _run(tmp_path, cone, "addc", "y", {"bad": {"add@nowhere.v:1.1-1.2": {"k": 2, "comp": "zero"}},
                                            "ok": {"out@y": {"k": 1, "comp": "zero"}}})
    assert "bad" in r["errors"] and "ok" in r["written"]


def test_menu_and_neighbours_are_deterministic_and_bounded():
    sites = [{"id": f"mul@f.v:{i}.1-{i}.5", "kind": "mul", "y_width": 32, "a": {}, "b": {}, "max_k": 15, "weight": 1000 - i}
             for i in range(30)] + [{"id": "out@y", "kind": "out", "y_width": 16, "a": None, "b": None, "max_k": 15, "weight": 16}]
    assert len(ranked_sites(sites)) == 16
    m1, m2 = build_menu(sites), build_menu(sites)
    assert m1 == m2 and "trunc_all_k4_mid" in m1 and "out_k3_max" in m1
    assert all(len(g) <= 16 for g in m1.values())
    g = {sites[0]["id"]: {"k": 4, "comp": "mid"}}
    n = neighbours(g, sites)
    assert any(v[sites[0]["id"]]["k"] == 5 for v in n.values())          # deepened
    assert any(len(v) == 2 for v in n.values())                          # a new gene
    assert all(v[sites[0]["id"]]["comp"] == "mid" for v in n.values() if len(v) == 1)
    assert next_k(12, 14) == 14 and next_k(14, 14) is None and next_k(0, 15) == 1


# ---------------------------------------------------------------------------------------------------------------
# Property: approximation never changes the fan-in of a precise signal (Phase 5 gate).
# ---------------------------------------------------------------------------------------------------------------
TOY = """module toy(
  input clk,
  input rst,
  (* axc_precise *) input en,
  input [7:0] a,
  input [7:0] b,
  (* axc_precise *) output reg vld,
  (* axc_relax, axc_bound = 64, axc_format = "u17" *) output reg [16:0] y
);
  reg [3:0] cnt;
  always @(posedge clk) begin
    if (rst) begin y <= 17'd0; vld <= 1'b0; cnt <= 4'd0; end
    else begin
      vld <= en & (cnt != 4'd0);
      cnt <= cnt + {3'd0, en};
      y <= a * 8'd13 + {9'd0, b};
    end
  end
endmodule
"""


def _extract_and_reintegrate(tmp_path, genome_builder):
    from axfpga.cone_extractor import ConeExtractor
    from axfpga.operators import run_operators
    from axfpga.reintegrator import Reintegrator
    src = tmp_path / "toy.v"
    src.write_text(TOY)
    cone, shell, meta_p = tmp_path / "cone.v", tmp_path / "shell.v", tmp_path / "meta.json"
    meta = ConeExtractor(src).extract_safe_cone(out_submod_path=cone, out_top_path=shell, out_meta_path=meta_p)
    mod = meta["submodule_module"]
    out_wire = meta["submodule_outputs"][0]["name"]
    sites = run_operators(cone, mod, out_wire, {}, tmp_path / "ops", want_sites=True)["sites"]
    genome = genome_builder(sites)
    res = run_operators(cone, mod, out_wire, {"g": genome}, tmp_path / "ops")
    assert not res["errors"], res["errors"]
    final = tmp_path / "final.v"
    Reintegrator(top_shell_path=shell, approx_cone_path=res["written"]["g"], extracted_cone_path=cone).reintegrate(out_final_path=final)
    # The reference is the same shell with the unapproximated cone: the extractor's own elaboration already restructures
    # the shell, so the original source is not comparable cell by cell.
    exact = tmp_path / "exact_reintegrated.v"
    Reintegrator(top_shell_path=shell, approx_cone_path=cone, extracted_cone_path=cone).reintegrate(out_final_path=exact)
    return src, exact, final, sites, meta


@pytest.mark.parametrize("builder_name", ["arith", "arith_and_out", "all_deep"])
def test_precise_fanin_unchanged_by_operators(tmp_path, builder_name):
    from axfpga.operators.fanin import fanin_hashes, precise_signals

    def arith(sites):
        return {s["id"]: {"k": 3, "comp": "mid"} for s in sites if s["kind"] != "out" and s["max_k"] >= 3}

    def arith_and_out(sites):
        g = arith(sites)
        g.update({s["id"]: {"k": 2, "comp": "max"} for s in sites if s["kind"] == "out"})
        return g

    def all_deep(sites):
        return {s["id"]: {"k": min(s["max_k"], 12), "comp": "zero"} for s in sites if s["kind"] != "out" and s["max_k"] >= 1}

    src, exact, final, sites, meta = _extract_and_reintegrate(tmp_path, {"arith": arith, "arith_and_out": arith_and_out, "all_deep": all_deep}[builder_name])
    assert any(s["kind"] != "out" for s in sites), "the toy cone must contain arithmetic to approximate"
    precise = precise_signals(str(src), "toy")
    assert set(precise) >= {"en", "vld"}
    before = fanin_hashes(str(exact), "toy", precise)
    after = fanin_hashes(str(final), "toy", precise)
    assert before == after
    # not vacuous: the relaxed output's fan-in did change
    assert fanin_hashes(str(exact), "toy", ["y"]) != fanin_hashes(str(final), "toy", ["y"])


def test_fanin_hash_detects_a_change_in_a_precise_cone(tmp_path):
    from axfpga.operators.fanin import fanin_hashes
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_text("module m(input [3:0] x, input [3:0] z, output [3:0] p); assign p = x + z; endmodule\n")
    b.write_text("module m(input [3:0] x, input [3:0] z, output [3:0] p); assign p = x ^ z; endmodule\n")
    assert fanin_hashes(str(a), "m", ["p"]) != fanin_hashes(str(b), "m", ["p"])
    c = tmp_path / "c.v"
    c.write_text("module m(input [3:0] z, input [3:0] x, output [3:0] p); wire [3:0] t = x; assign p = t + z; endmodule\n")
    assert fanin_hashes(str(a), "m", ["p"]) == fanin_hashes(str(c), "m", ["p"])      # names and order do not matter
