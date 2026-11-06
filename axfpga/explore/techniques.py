"""One search space per approximation technique.

A technique knows its sites, the integer range of each gene, how to turn a genome into a candidate netlist, and how
many genomes its space holds (spaces no larger than the evaluation budget are enumerated exhaustively).

Genome formats:
  site techniques   {site id: {"op", "k", "comp"}}   (k = 0: site untouched)
  lut               {"lut@<i>": {"k": gene}}          (gene 0 exact .. 4 constant zero, see lutnet.GENE_NAMES)
  catalog           {"index": i}                      (one whole-cone candidate of a fixed list)
  ppg_design        {"k": depth}                      (anti-diagonal depth of every array multiplier)
  trunc_reg         {site id: {"k", "comp"}}          (design level, behind registers)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from axfpga.explore import lutnet

COMP_SET = ("zero", "mid")


def genome_key(g: Dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in sorted(g.items()) if not (isinstance(v, dict) and int(v.get("k", 0)) == 0)},
                      sort_keys=True)


def _short(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class Technique:
    name = "base"
    level = "cone"             # "cone" (ConeEvaluator) or "design" (DesignEvaluator)
    method = "search"          # "search" (mealpy / exhaustive), "bisect" (monotone 1-D depth)
    description = ""

    def __init__(self) -> None:
        self.genes: List[Dict[str, Any]] = []   # [{"site": id, "op": op, "max_k": int, "comp": bool, ...}]

    # space -----------------------------------------------------------------
    def size(self) -> int:
        n = 1
        for g in self.genes:
            n *= 1 + g["max_k"] * (len(COMP_SET) if g.get("comp") else 1)
            if n > 10 ** 12:
                return n
        return n

    def dims(self) -> int:
        return len(self.genes)

    def vector_to_genome(self, ks: Sequence[int], comps: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        g: Dict[str, Any] = {}
        ci = 0
        for gene, k in zip(self.genes, ks):
            comp = "zero"
            if gene.get("comp"):
                comp = comps[ci] if comps is not None else "zero"
                ci += 1
            if int(k) > 0:
                g[gene["site"]] = {"op": gene["op"], "k": int(k), "comp": comp}
        return g

    def all_genomes(self) -> Iterable[Dict[str, Any]]:
        ranges = []
        for gene in self.genes:
            opts = [(0, "zero")]
            for k in range(1, gene["max_k"] + 1):
                for c in (COMP_SET if gene.get("comp") else ("zero",)):
                    opts.append((k, c))
            ranges.append(opts)
        for combo in itertools.product(*ranges):
            yield {gene["site"]: {"op": gene["op"], "k": k, "comp": c}
                   for gene, (k, c) in zip(self.genes, combo) if k > 0}

    def single_site_genomes(self, ks_per_site: int = 2) -> List[Dict[str, Any]]:
        """One-site-at-a-time genomes for the explanation sweep: each site alone at a light and a deep setting."""
        out = []
        for gene in self.genes:
            mk = gene["max_k"]
            for k in sorted({1, max(1, (mk + 1) // 2)} if ks_per_site >= 2 else {1}):
                if 1 <= k <= mk:
                    out.append({gene["site"]: {"op": gene["op"], "k": k, "comp": "zero"}})
        return out

    # candidates -----------------------------------------------------------
    def materialize(self, genomes: Dict[str, Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
        raise NotImplementedError

    def describe(self, genome: Dict[str, Any]) -> str:
        parts = [f"{sid}:{v.get('op', '')}{v['k']}{('/' + v['comp']) if v.get('comp', 'zero') != 'zero' else ''}"
                 for sid, v in sorted(genome.items()) if isinstance(v, dict) and v.get("k", 0)]
        return ", ".join(parts) if parts else "exact"


class SiteOpsTechnique(Technique):
    """Per-site RTL operators (axfpga.explore.ops) on the word-level cone."""

    OPS_OF = {"trunc": ("trunc", "wire", "out"), "loa": ("loa",), "nibble": ("nibble",), "ppg": ("ppg",)}
    DESCR = {"trunc": "LSB truncation of operands, named wires and the output bus, with correction constant",
             "loa": "lower-part OR adder per adder", "nibble": "segmented carry (4-bit segments without carries) per adder",
             "ppg": "partial-product pruning per multiplier"}

    def __init__(self, name: str, sites: Sequence[Dict[str, Any]], cone_v: Path, module: str,
                 out_wire: Optional[str], max_sites: int = 40):
        super().__init__()
        self.name = name
        self.description = self.DESCR[name]
        self.cone_v, self.module, self.out_wire = Path(cone_v), module, out_wire
        self.sites = []
        for s in sites:
            ops = [o for o in s.get("ops", []) if o in self.OPS_OF[name]]
            if not ops:
                continue
            op = ops[0]
            mk = {"trunc": s["max_k"], "wire": s["max_k"], "out": s["max_k"], "loa": s.get("max_loa", 0),
                  "nibble": s.get("max_nibble", 0), "ppg": s.get("max_ppg", 0)}[op]
            mk = min(int(mk), 24)
            if mk < 1:
                continue
            self.sites.append(s)
            self.genes.append({"site": s["id"], "op": op, "max_k": mk, "comp": op in ("trunc", "wire", "out"),
                               "kind": s["kind"], "width": s.get("y_width")})
        if len(self.genes) > max_sites:
            # the output bus is always kept (it is the cheapest site to reason about and often the best one);
            # the other sites are ranked by weight
            ranked = sorted(range(len(self.genes)), key=lambda i: (self.sites[i]["kind"] != "out",
                                                                   -(self.sites[i].get("weight") or 0), self.genes[i]["site"]))
            keep = ranked[:max_sites]
            keep = sorted(keep)
            self.genes = [self.genes[i] for i in keep]
            self.sites = [self.sites[i] for i in keep]

    def materialize(self, genomes: Dict[str, Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = _short(json.dumps(sorted(genomes), sort_keys=True) + str(threading.get_ident()))
        spec = {"cone": str(self.cone_v), "module": self.module, "out_wire": self.out_wire,
                "genomes": genomes, "out_dir": str(out_dir)}
        sp, rp = out_dir / f"spec_{tag}.json", out_dir / f"res_{tag}.json"
        sp.write_text(json.dumps(spec))
        proc = subprocess.run([sys.executable, "-m", "axfpga.explore.ops", str(sp), str(rp)], capture_output=True,
                              text=True, timeout=1800)
        sp.unlink(missing_ok=True)
        if proc.returncode != 0 or not rp.exists():
            raise RuntimeError(f"ops batch failed: {proc.stderr[-400:]}")
        res = json.loads(rp.read_text())
        rp.unlink(missing_ok=True)
        return {n: Path(p) for n, p in res["written"].items()}


class LutTechnique(Technique):
    """Per-LUT approximate rewrites (pyALS catalog) on the cone's 4-LUT mapping."""

    name = "pyals_lut"
    description = "per-LUT approximate rewrite (pyALS catalog: distance 1/2, input dropping, constant 0)"

    def __init__(self, net: lutnet.LutNet, catalog: Any):
        super().__init__()
        self.net, self.catalog = net, catalog
        self.sites = net.sites
        self._lock = threading.Lock()
        for s in net.sites:
            self.genes.append({"site": s["id"], "op": "lut", "max_k": s["max_k"], "comp": False, "kind": "lut",
                               "significance": s["significance"]})

    def materialize(self, genomes: Dict[str, Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for name, g in genomes.items():
            genes = {int(sid.split("@")[1]): int(v["k"]) for sid, v in g.items() if int(v.get("k", 0)) > 0}
            with self._lock:                       # the catalog cache is a sqlite connection
                text, _ = self.net.apply(genes, self.catalog)
            p = out_dir / f"{name}.v"
            p.write_text(text)
            out[name] = p
        return out

    def describe(self, genome: Dict[str, Any]) -> str:
        parts = [f"{sid}:{lutnet.GENE_NAMES[int(v['k'])]}" for sid, v in sorted(genome.items()) if int(v.get("k", 0)) > 0]
        return ", ".join(parts) if parts else "exact"


class CatalogTechnique(Technique):
    """A fixed list of whole-cone candidates (techniques that rewrite the whole cone with one parameter)."""

    method = "enumerate"

    def __init__(self, name: str, description: str, candidates: Sequence[Tuple[str, Path]]):
        super().__init__()
        self.name, self.description = name, description
        self.candidates = list(candidates)
        self.sites = []

    def size(self) -> int:
        return len(self.candidates)

    def dims(self) -> int:
        return 1

    def all_genomes(self) -> Iterable[Dict[str, Any]]:
        for i in range(len(self.candidates)):
            yield {"index": i}

    def single_site_genomes(self, ks_per_site: int = 2) -> List[Dict[str, Any]]:
        return []

    def materialize(self, genomes: Dict[str, Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
        return {n: Path(self.candidates[int(g["index"])][1]) for n, g in genomes.items()}

    def describe(self, genome: Dict[str, Any]) -> str:
        return self.candidates[int(genome["index"])][0] if "index" in genome else "exact"


class PpgDesignTechnique(Technique):
    """Anti-diagonal pruning depth of every array multiplier of the design (UB1BPPG cells), one integer."""

    name = "ppg_design"
    level = "design"
    method = "bisect"
    description = "partial-product pruning of the design's array multipliers (anti-diagonal depth)"

    def __init__(self, code: str, k_max: int, pruner: Any):
        super().__init__()
        self.code, self.k_max, self.pruner = code, k_max, pruner
        self.sites = []
        self.genes = [{"site": "ppg@design", "op": "ppg", "max_k": k_max, "comp": False, "kind": "ppg"}]

    def size(self) -> int:
        return self.k_max

    def all_genomes(self) -> Iterable[Dict[str, Any]]:
        for k in range(1, self.k_max + 1):
            yield {"k": k}

    def single_site_genomes(self, ks_per_site: int = 2) -> List[Dict[str, Any]]:
        return []

    def materialize(self, genomes: Dict[str, Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for name, g in genomes.items():
            code, _ = self.pruner.prune_code_ppg(self.code, int(g["k"]))
            p = out_dir / f"{name}.v"
            p.write_text(code)
            out[name] = p
        return out

    def describe(self, genome: Dict[str, Any]) -> str:
        return f"anti-diagonal depth {genome.get('k')}"


class RegTechnique(Technique):
    """Operand truncation of the arithmetic behind internal registers, per site (design level)."""

    name = "trunc_reg"
    level = "design"
    description = "LSB truncation of arithmetic behind internal registers (per site)"

    def __init__(self, files: Sequence[str], top: str, sites: Sequence[str], max_k: int = 8, max_sites: int = 24):
        super().__init__()
        self.files, self.top = list(files), top
        self.sites = [{"id": s, "kind": s.split("@")[0]} for s in list(sites)[:max_sites]]
        self.genes = [{"site": s["id"], "op": "trunc", "max_k": max_k, "comp": True, "kind": s["kind"]} for s in self.sites]

    def materialize(self, genomes: Dict[str, Dict[str, Any]], out_dir: Path) -> Dict[str, Path]:
        from axfpga.operators.design_level import run_design_level
        out_dir.mkdir(parents=True, exist_ok=True)
        sub = out_dir / _short(json.dumps(sorted(genomes), sort_keys=True) + str(threading.get_ident()))
        spec = {n: {"sites": {sid: {"k": v["k"], "comp": v.get("comp", "zero")} for sid, v in g.items()}}
                for n, g in genomes.items()}
        res = run_design_level(self.files, self.top, [s["id"] for s in self.sites], spec, str(sub))
        return {n: Path(p) for n, p in res["written"].items()}
