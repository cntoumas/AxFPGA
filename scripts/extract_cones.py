#!/usr/bin/env python3
"""Stage 2 of the pipeline only: annotate each design and extract its safe cone, as run_design does."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def extract(bench: dict, out: Path) -> dict:
    from axfpga.cone_extractor import ConeExtractor
    from axfpga.pipeline import DesignSpec, _patched_files, annotate_verilog

    spec = DesignSpec.from_dict(bench)
    work = out / spec.id
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    files = _patched_files(spec, work)
    b = dataclasses.asdict(spec)
    adir = work / "annotated_rtl"
    adir.mkdir()
    if b["annotated"]:
        annotated = []
        for f in files:
            shutil.copy(f, adir / f.name)
            annotated.append(adir / f.name)
    else:
        annotated = annotate_verilog(input_files=files, output_dir=adir, top_module=spec.top,
                                     relax_wire=b["relax_wire"], precise_signals=b["precise_signals"], bound=b["bound"],
                                     metric=b["metric"], attrs=b["quality_attrs"], clock=b["clock"], reset=b["reset"],
                                     reset_active_low=b["reset_active_low"])
    through = bool(spec.through_registers and os.environ.get("AXFPGA_THROUGH_REGISTERS") != "0")
    meta = ConeExtractor(annotated, top_module=spec.top, through_registers=through).extract_safe_cone(
        submod_tag=f"{spec.top}_axc_cone_0", out_submod_path=work / "extracted_cone.v",
        out_top_path=work / "top_shell.v", out_meta_path=work / "cone_meta.json")
    if b["annotated"] and meta.get("relaxed_signals"):          # same budget override as run_design
        for sig in meta["relaxed_signals"]:
            sig["bound"] = b["bound"]
        (work / "cone_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"id": spec.id, "status": meta.get("status"), "safe_cells": len(meta.get("safe_cells", []))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("-b", "--benchmark", action="append", required=True)
    a = ap.parse_args()
    import run_pyals_yosys_suite as suite
    benches = {b["id"]: b for b in suite.define_benchmark_suite()}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for bid in a.benchmark:
        t0 = time.time()
        try:
            r = extract(benches[bid], out)
            print(f"{bid}: {r['status']} | {r['safe_cells']} safe cells | {time.time() - t0:.0f}s", flush=True)
        except Exception as exc:
            print(f"{bid}: FAILED {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    main()
