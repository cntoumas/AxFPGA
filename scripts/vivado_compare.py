#!/usr/bin/env python3
"""Vivado implementation of the exact and the approximate design of every benchmark of a suite run."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

CLK_NS = 8.0
VARIANTS = {"exact_rtl": "exact_baseline_top.v", "approx_yosys": "approx_mapped.v",
            "exact_yosys": "exact_mapped.v", "approx_rtl": "final_approx_top.v"}
APPROX = ("approx_yosys", "approx_rtl")


def _clock_of(design_v: Path, top: str, bench: Optional[Dict[str, Any]]) -> Optional[str]:
    from axfpga.pipeline import _top_ports
    b = bench or {}
    return _top_ports([design_v], top, b.get("clock"), b.get("reset"), b.get("reset_active_low"))["clk"]


def tcl_script(files: List[Path], top: str, part: str, clk: Optional[str], threads: int) -> str:
    reads = "\n".join(f'read_verilog -sv {{{f.resolve()}}}' for f in files)
    if clk:
        clock = (f"create_clock -name clk -period {CLK_NS} [get_ports {{{clk}}}]\n"
                 f"set data_in [get_ports -filter {{DIRECTION == IN && NAME != \"{clk}\"}}]")
    else:
        clock = f"create_clock -name clk -period {CLK_NS}\nset data_in [get_ports -filter {{DIRECTION == IN}}]"
    return f"""set_param general.maxThreads {threads}
{reads}
synth_design -top {top} -part {part} -mode out_of_context
{clock}
if {{[llength $data_in]}} {{ set_input_delay -clock clk 0 $data_in }}
set data_out [get_ports -filter {{DIRECTION == OUT}}]
if {{[llength $data_out]}} {{ set_output_delay -clock clk 0 $data_out }}
opt_design
place_design
route_design
report_utilization -file util.rpt
report_timing_summary -max_paths 1 -file timing.rpt
set_units -power mW
report_power -file power.rpt
set p [get_timing_paths -max_paths 1 -nworst 1 -setup]
if {{[llength $p]}} {{
  puts "AXV slack [get_property SLACK $p]"
  puts "AXV datapath [get_property DATAPATH_DELAY $p]"
  puts "AXV levels [get_property LOGIC_LEVELS $p]"
}}
puts "AXV done"
"""


def _num(pat: str, text: str) -> Optional[float]:
    m = re.search(pat, text, re.MULTILINE)
    return float(m.group(1)) if m else None


def parse(d: Path, log: str) -> Dict[str, Any]:
    u = (d / "util.rpt").read_text(errors="ignore") if (d / "util.rpt").exists() else ""
    pw = (d / "power.rpt").read_text(errors="ignore") if (d / "power.rpt").exists() else ""
    def row(*names: str) -> Optional[float]:          # first table row that exists (0 is a value, not missing)
        for name in names:
            v = _num(rf"^\|\s*{name}\s*\|\s*([\d.]+)\s*\|", u)
            if v is not None:
                return v
        return None
    r: Dict[str, Any] = {
        "luts": row(r"Slice LUTs\*?", r"CLB LUTs\*?"),
        "lut_logic": row(r"LUT as Logic"),
        "lut_mem": row(r"LUT as Memory"),
        "ffs": row(r"Slice Registers", r"CLB Registers"),
        "muxf7": row(r"F7 Muxes"), "muxf8": row(r"F8 Muxes"),
        "carry4": row(r"CARRY4") if not u or re.search(r"^\|\s*CARRY4\s*\|", u, re.M) else 0,   # listed only when used
        "slices": row(r"Slice"),
        "dsps": row(r"DSPs"),
        "bram_tiles": row(r"Block RAM Tile"),
        "power_total_mw": _num(r"Total On-Chip Power \(mW\)\s*\|\s*([\d.]+)", pw),
        "power_dynamic_mw": _num(r"^\|\s*Dynamic \(mW\)\s*\|\s*([\d.]+)", pw),
        "power_static_mw": _num(r"Device Static \(mW\)\s*\|\s*([\d.]+)", pw),
        "slack_ns": _num(r"^AXV slack (-?[\d.]+)", log),
        "datapath_ns": _num(r"^AXV datapath ([\d.]+)", log),
        "logic_levels": _num(r"^AXV levels (\d+)", log),
    }
    for k in ("luts", "ffs", "carry4", "slices", "dsps", "muxf7", "muxf8"):
        if r[k] is not None:
            r[k] = int(r[k])
    if r["slack_ns"] is not None:
        r["fmax_mhz"] = round(1000.0 / (CLK_NS - r["slack_ns"]), 2)
    if r["luts"] is not None:
        r["lc"] = r["luts"] + 2 * (r["carry4"] or 0)        # the framework's logic-cell measure
    r["ok"] = "AXV done" in log and r["luts"] is not None
    return r


def run_one(job: Dict[str, Any]) -> Dict[str, Any]:
    d = Path(job["dir"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.tcl").write_text(tcl_script([Path(f) for f in job["files"]], job["top"], job["part"], job["clk"],
                                          job["threads"]))
    t0 = time.time()
    try:
        p = subprocess.run([job["vivado"], "-mode", "batch", "-nojournal", "-log", "vivado.log", "-source", "run.tcl"],
                           cwd=d, capture_output=True, text=True, timeout=job["timeout"])
        log, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        log, rc = f"timeout after {job['timeout']} s\n{exc.stdout or ''}", -1
    res = parse(d, log)
    res.update(design=job["design"], variant=job["variant"], part=job["part"], clock=job["clk"], rc=rc,
               time_s=round(time.time() - t0, 1))
    if not res["ok"]:
        errs = [ln for ln in log.splitlines() if ln.startswith(("ERROR", "CRITICAL WARNING"))]
        res["error"] = ("; ".join(errs[:3]) or log[-400:])[:600]
    return res


def _pct(e: Optional[float], a: Optional[float]) -> Optional[float]:
    return None if e in (None, 0) or a is None else round(100.0 * (e - a) / e, 2)


def report(rows: List[Dict[str, Any]], results: Dict[str, Dict[str, Any]], out: Path) -> None:
    by = {(r["design"], r["variant"]): r for r in rows}
    designs = sorted({r["design"] for r in rows})
    keys = [("lc", "LC"), ("luts", "LUT"), ("ffs", "FF"), ("carry4", "CARRY4"), ("dsps", "DSP"), ("slices", "Slices"),
            ("power_dynamic_mw", "Dyn. power mW"), ("fmax_mhz", "Fmax")]
    pairs = [("exact_rtl", "approx_yosys", "Requested: exact RTL (Vivado) vs approximate Yosys netlist (Vivado)"),
             ("exact_yosys", "approx_yosys", "Control A: both Yosys netlists through Vivado (approximation only)"),
             ("exact_rtl", "approx_rtl", "Control B: both RTL through Vivado synthesis (approximation only)"),
             ("exact_rtl", "exact_yosys", "Mapping effect: exact RTL, Vivado synthesis vs Yosys mapping")]
    md = ["# Vivado implementation: exact vs approximate", "",
          f"Vivado out-of-context synth + place + route, clock {CLK_NS} ns ({1000 / CLK_NS:.0f} MHz) on every variant. "
          "LC = LUT + 2 x CARRY4 (the framework's measure). Power: Vivado vectorless estimate (default switching "
          "activity), no I/O power (out of context). Fmax = 1 / (period - WNS). Saved % = (exact - approx) / exact; "
          "for Fmax a negative value is a slower design.", ""]
    summary: Dict[str, Any] = {}
    for a, b, title in pairs:
        md += [f"## {title}", "",
               "| Design | " + " | ".join(f"{lab} {a.split('_')[0]} -> {b.split('_')[0]}" for _, lab in keys)
               + " | Yosys LC saved % (suite) |", "|:--" + "|--:" * (len(keys) + 1) + "|"]
        sums: Dict[str, List[float]] = {k: [] for k, _ in keys}
        for d in designs:
            ra, rb = by.get((d, a)), by.get((d, b))
            if not ra or not rb or not ra["ok"] or not rb["ok"]:
                continue
            cells = []
            for k, _ in keys:
                ea, eb = ra.get(k), rb.get(k)
                pc = _pct(ea, eb)
                if k == "fmax_mhz" and pc is not None:
                    pc = -pc                         # higher is better: positive = faster
                if pc is not None:
                    sums[k].append(pc)
                fmt = (lambda v: f"{v:.3f}") if k == "power_dynamic_mw" else (lambda v: f"{v:g}")
                cells.append("-" if ea is None or eb is None else f"{fmt(ea)} -> {fmt(eb)} ({'-' if pc is None else f'{pc:+.1f}'} %)")
            ys = (results.get(d) or {}).get("savings_pct", {}).get("logic_cells")
            md.append(f"| {d} | " + " | ".join(cells) + f" | {'-' if ys is None else f'{ys:.1f}'} |")
        md.append("| **mean** | " + " | ".join(f"{statistics.mean(v):+.1f} %" if v else "-" for k, v in sums.items())
                  + " | |")
        md.append("")
        summary[f"{a}_vs_{b}"] = {k: (statistics.mean(v) if v else None) for k, v in sums.items()}
    failed = [r for r in rows if not r["ok"]]
    if failed:
        md += ["## Failed runs", ""] + [f"- {r['design']} {r['variant']}: {r.get('error', '')[:300]}" for r in failed]
    (out / "VIVADO_REPORT.md").write_text("\n".join(md) + "\n")
    (out / "vivado_summary.json").write_text(json.dumps(summary, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-build", required=True, help="per-design dirs with the RTL and Yosys netlists of a suite run")
    ap.add_argument("--run-results", required=True, help="results.json of the same run")
    ap.add_argument("--out", required=True)
    ap.add_argument("--designs", default="", help="comma-separated ids (default: every design of the run)")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--vivado", default=os.environ.get("AXFPGA_VIVADO", "vivado"))
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--threads", type=int, default=2, help="Vivado threads per run")
    ap.add_argument("--timeout", type=int, default=6 * 3600)
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {r["id"]: r for r in json.loads(Path(a.run_results).read_text())}
    part_file = out / "vivado_runs.jsonl"
    rows = [json.loads(ln) for ln in part_file.read_text().splitlines() if ln.strip()] if part_file.exists() else []
    if not a.report_only:
        try:
            import run_pyals_yosys_suite as suite
            benches = {b["id"]: b for b in suite.define_benchmark_suite()}
        except Exception as exc:                       # clock / reset fall back to the name guess
            print("suite definitions unavailable:", exc)
            benches = {}
        done = {(r["design"], r["variant"]) for r in rows if r["ok"]}
        rows = [r for r in rows if r["ok"]]
        jobs = []
        ids = [d for d in (a.designs.split(",") if a.designs else sorted(results)) if d]
        for d in ids:
            r, bdir = results.get(d), Path(a.run_build) / d
            if r is None or not (bdir / VARIANTS["exact_rtl"]).exists():
                print(f"skip {d}: no result or no netlists in {bdir}")
                continue
            clk = _clock_of(bdir / VARIANTS["exact_rtl"], r["top"], benches.get(d))
            for v in a.variants.split(","):
                if (d, v) in done or (v in APPROX and r["fell_back"]):
                    continue
                f = bdir / VARIANTS[v]
                if not f.exists():
                    print(f"skip {d} {v}: {f.name} missing")
                    continue
                jobs.append({"design": d, "variant": v, "files": [str(f)], "top": r["top"], "part": r["device"],
                             "clk": clk, "dir": str(out / d / v), "vivado": a.vivado, "threads": a.threads,
                             "timeout": a.timeout})
        # biggest designs first: they set the wall time
        jobs.sort(key=lambda j: -Path(j["files"][0]).stat().st_size)
        print(f"{len(jobs)} Vivado run(s), {len(done)} already done", flush=True)
        with ProcessPoolExecutor(max_workers=a.parallel) as pool, open(part_file, "a") as fh:
            for f in as_completed([pool.submit(run_one, j) for j in jobs]):
                r = f.result()
                fh.write(json.dumps(r) + "\n")
                fh.flush()
                rows.append(r)
                print(f"{r['design']:22} {r['variant']:13} {'ok ' if r['ok'] else 'FAIL'} LC {r.get('lc')} "
                      f"slices {r.get('slices')} WNS {r.get('slack_ns')} dyn {r.get('power_dynamic_mw')} mW "
                      f"{r['time_s']:.0f}s {r.get('error', '')[:120]}", flush=True)
    (out / "vivado_results.json").write_text(json.dumps(rows, indent=1))
    report(rows, results, out)
    print("wrote", out / "VIVADO_REPORT.md")


if __name__ == "__main__":
    main()
