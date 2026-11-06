#!/usr/bin/env python3
"""Table of the Phase 5 ablation: logic cells saved per design and AXFPGA_OPS mode, against the anchor (mode ``off``).

Reads ``<dir>/<mode>/results.json`` (written by scripts/phase5_ablation.sh) and writes ``ablation.md`` / ``ablation.json``.
A saving counts only for results of tier ``certified`` or ``uncertified`` that met every constraint (every result in a
results.json did: otherwise the exact design is kept); the tier is shown next to the number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

MODES = ["off", "nocomp-nostack", "nocomp", "nostack", "all"]
LABEL = {"off": "no operators (anchor)", "nocomp-nostack": "truncation only", "nocomp": "+ stacking",
         "nostack": "+ correction", "all": "+ stacking + correction"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True, help="reports/phase5/ablation")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    data: Dict[str, Dict[str, Any]] = {}
    for mode in MODES:
        f = Path(args.dir) / mode / "results.json"
        if f.exists():
            data[mode] = {r["id"]: r for r in json.loads(f.read_text())}
    ids = sorted({i for m in data.values() for i in m})
    rows = []
    for i in ids:
        row = {"id": i}
        for mode in MODES:
            r = data.get(mode, {}).get(i)
            row[mode] = None if r is None else {"saved": r["savings_pct"]["logic_cells"], "tier": r["certification"]["tier"],
                                                "method": r["method"], "wce": r["errors"]["wce"], "limit": r["bound"]}
        rows.append(row)
    od = Path(args.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    (od / "ablation.json").write_text(json.dumps(rows, indent=1))
    md = ["# Operator ablation (DSP inference off): logic cells saved", "",
          "Each column adds one ingredient. `anchor` = the candidate set of Phases 3 and 4 with the Phase 5 Tournament fixes. "
          "Cells: LC saved % (tier: c = certified, u = uncertified), technique of the accepted design.", "",
          "| Design | " + " | ".join(LABEL[m] for m in MODES if m in data) + " |", "|:--|" + "--:|" * len([m for m in MODES if m in data])]
    wins: Dict[str, int] = {}
    for row in rows:
        cells = []
        for mode in MODES:
            if mode not in data:
                continue
            v = row[mode]
            cells.append("n/a" if v is None else f"{v['saved']:.1f}% ({v['tier'][0]}) `{v['method'].split(' ')[0][:22]}`")
        md.append(f"| {row['id']} | " + " | ".join(cells) + " |")
    def gain(a: str, b: str):
        return [(r["id"], r[b]["saved"] - r[a]["saved"]) for r in rows if r.get(a) and r.get(b) and r[b]["saved"] - r[a]["saved"] > 0.05]
    md += ["", "## Improvements", ""]
    for a, b, text in [("off", "all", "full operator set over the anchor"), ("nocomp-nostack", "nostack", "correction constants (single gene)"),
                       ("nocomp", "all", "correction constants (with stacking)"), ("nocomp-nostack", "nocomp", "stacking (no correction)"),
                       ("nostack", "all", "stacking (with correction)")]:
        if a in data and b in data:
            g = gain(a, b)
            md.append(f"- **{text}** ({LABEL[a]} -> {LABEL[b]}): " + (", ".join(f"{i} +{d:.1f} pp" for i, d in g) if g else "no design improves"))
    (od / "ablation.md").write_text("\n".join(md) + "\n")
    print(f"Wrote {od / 'ablation.md'}")


if __name__ == "__main__":
    main()
