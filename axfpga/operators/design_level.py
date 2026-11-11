"""Design-level operators: truncation of arithmetic cells of the whole elaborated design, behind registers included."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional


def run(spec: Dict[str, Any]) -> Dict[str, Any]:
    from pyosys import libyosys as ys

    from axfpga.cone_extractor import ConeExtractor
    from axfpga.operators.core import _site_cells, apply_genome

    result: Dict[str, Any] = {"written": {}, "errors": {}, "applied": {}, "missing": []}
    allowed = list(spec["sites"])
    for name, g in spec["genomes"].items():
        try:
            ex = ConeExtractor(spec["files"], top_module=spec["top"])
            module = ex.elaborate()
            present = {sid for sid, _, _ in _site_cells(module)}
            sites = [s for s in allowed if s in present]
            result["missing"] = sorted(set(allowed) - present)
            if "sites" in g:          # per-site genome {site id: {"k", "comp"}} (design-space exploration)
                genome = {s: {"k": int(v["k"]), "comp": v.get("comp", "zero")} for s, v in g["sites"].items()
                          if s in present and int(v.get("k", 0)) > 0}
            else:
                genome = {s: {"k": int(g["k"]), "comp": g.get("comp", "zero")} for s in sites}
            result["applied"][name] = len(apply_genome(module, genome, None))
            path = os.path.join(spec["out_dir"], f"design_{name}.v")
            ys.run_pass(f"opt_clean -purge; clean; write_verilog -noattr {path}", ex.design)
            result["written"][name] = path
        except Exception as exc:
            result["errors"][name] = f"{type(exc).__name__}: {exc}"
    return result


def run_design_level(files: List[str], top: str, sites: List[str], genomes: Dict[str, Dict[str, Any]], out_dir: str,
                     timeout: int = 900) -> Dict[str, Any]:
    """``run`` in a subprocess (keeps libyosys state out of the calling process)."""
    import subprocess
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sp, rp = out / "dl_spec.json", out / "dl_result.json"
    sp.write_text(json.dumps({"files": [str(f) for f in files], "top": top, "sites": sites, "genomes": genomes, "out_dir": str(out)}))
    rp.unlink(missing_ok=True)
    proc = subprocess.run([sys.executable, "-m", "axfpga.operators.design_level", str(sp), str(rp)],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not rp.exists():
        raise RuntimeError(f"design-level operator batch failed (rc {proc.returncode}): {proc.stderr[-400:]}")
    return json.loads(rp.read_text())


if __name__ == "__main__":
    spec = json.loads(open(sys.argv[1]).read())
    open(sys.argv[2], "w").write(json.dumps(run(spec)))
