#!/usr/bin/env python3
"""Automatic site lists (Phase 5): the approximation sites of every design, from its extracted cone.

Reads ``<build-dir>/<id>/extracted_cone.v`` and ``cone_meta.json`` of a suite run, enumerates the sites with
``axfpga.operators`` (arithmetic cells keyed by source location, plus the relaxed output bus) and writes
``sites.json`` and ``sites.md`` into ``--out-dir``. No design-specific knowledge is used.

Also reports how much of each design's arithmetic the cone reaches: ``cone cells`` are the sites in the extracted cone,
``design arithmetic`` the ``$add/$sub/$mul`` cells of the flattened design (what a site list could cover if the cone
reached behind internal registers).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from axfpga.operators import run_operators


def first_module(path: Path) -> str:
    m = re.search(r"^module\s+(\S+?)\s*\(", path.read_text(), re.M)
    if not m:
        raise ValueError(f"no module in {path}")
    return m.group(1)


def design_arith(annotated: List[Path], top: str) -> Dict[str, int]:
    """Arithmetic cells of the flattened design (Yosys, subprocess)."""
    script = " ".join(f"read_verilog -sv {f};" for f in annotated) + f" hierarchy -check -top {top}; proc; flatten; opt_clean; stat"
    res = subprocess.run([sys.executable, "-m", "axfpga.yosys_cli", "-p", script], capture_output=True, text=True)
    counts = {"$add": 0, "$sub": 0, "$mul": 0, "$alu": 0, "$macc": 0}
    for line in res.stdout.splitlines():
        m = re.match(r"^\s+(\d+)\s+(\$[a-z_]+)\s*$", line)
        if m and m.group(2) in counts:
            counts[m.group(2)] = int(m.group(1))
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-sites", type=int, default=16)
    args = ap.parse_args()
    bd, od = Path(args.build_dir), Path(args.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    scratch = od / "_scratch"
    rows: List[Dict[str, Any]] = []
    for d in sorted(p for p in bd.iterdir() if (p / "extracted_cone.v").exists()):
        meta = json.loads((d / "cone_meta.json").read_text())
        cone = d / "extracted_cone.v"
        mod = first_module(cone)
        out_wire = (meta.get("submodule_outputs") or [{}])[0].get("name") if meta.get("status") == "SUCCESS" else None
        try:
            res = run_operators(cone, mod, out_wire, {}, scratch / d.name, want_sites=True)
            sites = res["sites"]
            err = None
        except Exception as exc:               # a cone Yosys cannot read back is reported, not hidden
            sites, err = [], str(exc)[:200]
        annotated = sorted((d / "annotated_rtl").glob("*.v")) or sorted((d / "annotated_rtl").glob("*.sv"))
        total = design_arith(annotated, meta.get("top_module", "")) if annotated else {}
        arith = [s for s in sites if s["kind"] != "out"]
        truncatable = [s for s in arith if s["max_k"] >= 1]
        rows.append({"id": d.name, "cone_module": mod, "relaxed_output": out_wire, "status": meta.get("status"),
                     "cones": len(meta.get("cones", [])), "sites": sites, "error": err,
                     "design_arith": total, "n_design_arith": sum(total.values()),
                     "n_sites": len(sites), "n_arith": len(arith), "n_truncatable": len(truncatable),
                     "capped_at": min(len(truncatable), args.max_sites)})
    (od / "sites.json").write_text(json.dumps(rows, indent=2))
    md = ["# Site lists (generated automatically from each extracted cone)", "",
          "Arithmetic sites are keyed by the source location of the cell (`kind@file:line.col-line.col`); `out@<wire>` is the "
          "relaxed output bus. `max k` is the deepest operand truncation of the heaviest site. At most "
          f"{args.max_sites} arithmetic sites per design take part in the search (heaviest first).", "",
          "| Design | Cone status | Cones | Arithmetic cells in the cone | Design arithmetic cells | Truncatable | In the search | Output site | Heaviest site |",
          "|:--|:--|--:|--:|--:|--:|--:|:--|:--|"]
    for r in rows:
        arith = sorted((s for s in r["sites"] if s["kind"] != "out"), key=lambda s: -s["weight"])
        heavy = f"`{arith[0]['id']}` (max k {arith[0]['max_k']})" if arith else "none"
        out = next((s["id"] for s in r["sites"] if s["kind"] == "out"), "none")
        md.append(f"| {r['id']} | {r['status']} | {r['cones']} | {r['n_arith']} | {r['n_design_arith']} | {r['n_truncatable']} | {r['capped_at']} | "
                  f"`{out}` | {heavy} |")
        if r["error"]:
            md.append(f"| ^ error | {r['error']} | | | | | | | |")
    (od / "sites.md").write_text("\n".join(md) + "\n")
    print(f"Wrote {od / 'sites.md'}: {len(rows)} designs, {sum(r['n_sites'] for r in rows)} sites")


if __name__ == "__main__":
    main()
