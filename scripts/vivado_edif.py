#!/usr/bin/env python3
"""Yosys netlists as EDIF through Vivado place and route, post-route simulation and SAIF power, exact vs approximate."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
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

D_IN = 1.0          # ns: data inputs change this long after the clock edge at the reference register
D_OUT = 0.3         # ns: outputs must be valid this long before that edge (covers clock skew between registers)
GSR_NS = 200.0      # testbench idle time before the first clock edge (glbl holds GSR for 100 ns)
FUNC_SETUP = 1.0    # ns before the clock edge at which the functional reference samples the outputs
VARIANTS = {"exact": "exact_mapped.v", "approx": "approx_mapped.v"}


def ident(n: str) -> str:
    return n if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", n) else "\\" + n + " "


def run(cmd: List[str], cwd: Path, log: str, timeout: int) -> str:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as exc:
        out = f"timeout after {timeout} s\n{exc.stdout or ''}"
    (cwd / log).write_text(out)
    return out


def num(pat: str, text: str) -> Optional[float]:
    m = re.search(pat, text, re.M)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------------------------- implementation

def impl_tcl(edf: Path, top: str, part: str, clk: Optional[str], period: float, threads: int) -> str:
    if clk:
        cons = f"""create_clock -name clk -period {period} [get_ports {{{clk}}}]
set regs [all_registers -clock_pins]
set ffp [get_pins -quiet -filter {{REF_PIN_NAME == C}} $regs]
set ref [lindex [expr {{[llength $ffp] ? $ffp : $regs}}] 0]
set din [get_ports -filter {{DIRECTION == IN && NAME != "{clk}"}}]
if {{[llength $din]}} {{ set_input_delay -clock clk -reference_pin $ref {D_IN} $din }}
set dout [get_ports -filter {{DIRECTION == OUT}}]
if {{[llength $dout]}} {{ set_output_delay -clock clk -reference_pin $ref {D_OUT} $dout }}"""
        lat = """set q [get_timing_paths -quiet -max_paths 1 -setup -from [get_cells -of_objects $ref]]
if {[llength $q]} { puts "AXV latency [get_property STARTPOINT_CLOCK_DELAY $q]" } else {
  set q [get_timing_paths -quiet -max_paths 1 -setup -to [get_cells -of_objects $ref]]
  if {[llength $q]} { puts "AXV latency [get_property ENDPOINT_CLOCK_DELAY $q]" }
}
puts "AXV refpin $ref"
if {[llength $din]} {
  set ih [get_timing_paths -quiet -max_paths 1 -hold -from $din]
  if {[llength $ih]} { puts "AXV inhold slack [get_property SLACK $ih] srcclk [get_property STARTPOINT_CLOCK_DELAY $ih]" }
}"""
    else:
        cons = f"""create_clock -name clk -period {period}
set din [get_ports -filter {{DIRECTION == IN}}]
if {{[llength $din]}} {{ set_input_delay -clock clk 0 $din }}
set dout [get_ports -filter {{DIRECTION == OUT}}]
if {{[llength $dout]}} {{ set_output_delay -clock clk 0 $dout }}"""
        lat = ""
    return f"""set_param general.maxThreads {threads}
read_edif {{{edf}}}
link_design -part {part} -top {top} -mode out_of_context
{cons}
place_design
phys_opt_design
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
set h [get_timing_paths -max_paths 1 -nworst 1 -hold]
if {{[llength $h]}} {{ puts "AXV hold [get_property SLACK $h]" }}
{lat}
write_checkpoint -force post.dcp
write_verilog -mode timesim -sdf_anno true -force post.v
write_sdf -force post.sdf
write_verilog -mode funcsim -force post_func.v
puts "AXV done"
"""


def parse_util(d: Path) -> Dict[str, Any]:
    u = (d / "util.rpt").read_text(errors="ignore") if (d / "util.rpt").exists() else ""

    def row(*names: str) -> Optional[float]:
        for name in names:
            v = num(rf"^\|\s*{name}\s*\|\s*([\d.]+)\s*\|", u)
            if v is not None:
                return v
        return None

    def prim(name: str) -> int:
        v = num(rf"^\|\s*{name}\s*\|\s*(\d+)\s*\|", u)
        return int(v) if v is not None else 0

    r: Dict[str, Any] = {
        "luts": row(r"Slice LUTs\*?", r"CLB LUTs\*?"), "lut_mem": row(r"LUT as Memory"),
        "ffs": row(r"Slice Registers", r"CLB Registers"), "muxf7": row(r"F7 Muxes"), "muxf8": row(r"F8 Muxes"),
        "carry4": prim("CARRY4"), "slices": row(r"Slice"), "dsps": row(r"DSPs"), "bram_tiles": row(r"Block RAM Tile"),
        "ramb18": prim("RAMB18E1"), "ramb36": prim("RAMB36E1"), "bufg": prim("BUFG"),
        "lut_cells": sum(prim(f"LUT{i}") for i in range(1, 7)),      # netlist LUTs; "luts" = LUT sites after packing
    }
    for k in ("luts", "ffs", "slices", "dsps", "muxf7", "muxf8", "lut_mem"):
        if r[k] is not None:
            r[k] = int(r[k])
    if r["luts"] is not None:
        r["lc"] = r["luts"] + 2 * r["carry4"]                       # on occupied LUT sites
        r["lc_cells"] = r["lut_cells"] + 2 * r["carry4"]            # on netlist LUT cells (the suite's measure)
    return r


def sdf_clock_arrivals(sdf: Path) -> Optional[List[float]]:
    """[earliest, latest] clock arrival (ns, max delays) at the sequential cells, from the port through the BUFG."""
    text = sdf.read_text(errors="ignore")
    mx = r"\(\s*[-\d.]*:[-\d.]*:([-\d.]+)\s*\)"
    bufg = {}
    for m in re.finditer(r'\(CELLTYPE "BUFG"\)\s*\(INSTANCE ([^)]+)\)(.*?)\n\(CELL', text, re.S):
        io = re.search(r"\(IOPATH I O " + mx, m.group(2))
        bufg[m.group(1).strip()] = float(io.group(1)) / 1000 if io else 0.0
    arr = []
    for name, d in bufg.items():
        for m in re.finditer(r"\(INTERCONNECT " + re.escape(name) + r"/O (\S+) " + mx, text):
            arr.append(d + float(m.group(2)) / 1000)
    return [round(min(arr), 3), round(max(arr), 3)] if arr else None


def impl_one(job: Dict[str, Any]) -> Dict[str, Any]:
    d = Path(job["dir"])
    d.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    res: Dict[str, Any] = dict(design=job["design"], variant=job["variant"], ok=False, clock=job["clk"],
                               clk_ns=job["period"])
    edf = d / f"{job['top']}.edf"
    (d / "edif.ys").write_text(
        "read_verilog -lib +/xilinx/cells_sim.v\nread_verilog -lib +/xilinx/cells_xtra.v\n"
        f"read_verilog {job['netlist']}\nhierarchy -top {job['top']}\nsetundef -zero -params\n"
        f"write_edif -pvector bra {edf.resolve()}\n")
    yp = subprocess.run([sys.executable, "-m", "axfpga.yosys_cli", "-q", "-s", str((d / "edif.ys").resolve())], cwd=d,
                        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
    out = yp.stdout + yp.stderr
    (d / "edif.log").write_text(out)
    if not edf.exists():
        res["error"] = "edif failed: " + out[-300:]
        return res
    (d / "impl.tcl").write_text(impl_tcl(edf.resolve(), job["top"], job["part"], job["clk"], job["period"],
                                         job["threads"]))
    log = run([job["vivado"], "-mode", "batch", "-nojournal", "-log", "impl.log", "-source", "impl.tcl"], d,
              "impl.out", job["timeout"])
    res.update(parse_util(d))
    pw = (d / "power.rpt").read_text(errors="ignore") if (d / "power.rpt").exists() else ""
    res.update(vectorless_dyn_mw=num(r"^\|\s*Dynamic \(mW\)\s*\|\s*([\d.]+)", pw),
               wns=num(r"^AXV slack (-?[\d.]+)", log), whs=num(r"^AXV hold (-?[\d.]+)", log),
               datapath_ns=num(r"^AXV datapath ([\d.]+)", log), logic_levels=num(r"^AXV levels (\d+)", log),
               latency=num(r"^AXV latency (-?[\d.]+)", log),
               inhold_srcclk=num(r"^AXV inhold slack -?[\d.]+ srcclk (-?[\d.]+)", log))
    res["crit_warnings"] = len(re.findall(r"^CRITICAL WARNING", log, re.M))
    if job["clk"] and (d / "post.sdf").exists():
        res["clk_arrival"] = sdf_clock_arrivals(d / "post.sdf")
    res["ok"] = "AXV done" in log and res.get("luts") is not None and res["wns"] is not None
    if res["ok"]:
        res["fmax_mhz"] = round(1000.0 / (job["period"] - res["wns"]), 2)
    else:
        errs = [ln for ln in log.splitlines() if ln.startswith(("ERROR", "CRITICAL WARNING"))]
        res["error"] = ("; ".join(errs[:3]) or log[-400:])[:600]
    res["impl_s"] = round(time.time() - t0, 1)
    (d / "impl.json").write_text(json.dumps(res, indent=1))
    return res


# ------------------------------------------------------------------------------------------------- simulation

def tb_text(top: str, ports: Dict[str, Any], vec: str, period: float, lat: List[float], func: bool) -> str:
    ins, outs, clk, rst = ports["inputs"], ports["outputs"], ports["clk"], ports["rst"]
    safe = lambda n: re.sub(r"[^A-Za-z0-9_]", "_", n)
    L = ["`timescale 1ns/1ps", "module tb;"]
    for p in (clk, rst):
        if p:
            L.append(f"  reg {ident(p)};")
    L += [f"  reg [{w - 1}:0] in_{safe(n)};" for n, w in ins] + [f"  wire [{w - 1}:0] out_{safe(n)};" for n, w in outs]
    conn = [f".{ident(p)}({ident(p)})" for p in (clk, rst) if p]
    conn += [f".{ident(n)}(in_{safe(n)})" for n, _ in ins] + [f".{ident(n)}(out_{safe(n)})" for n, _ in outs]
    L.append(f"  {top} dut ({', '.join(conn)});")
    on, off = ("1'b0", "1'b1") if ports["rst_active_low"] else ("1'b1", "1'b0")
    half = period / 2
    rd = f'r = $fscanf(fi, "{" ".join(["%h"] * len(ins))}\\n", {", ".join("in_" + safe(n) for n, _ in ins)});'
    wr = f'$fwrite(fo, "{" ".join(["%h"] * len(outs))}\\n", {", ".join("out_" + safe(n) for n, _ in outs)});'
    L.append("  integer fi, fo, r;")
    if clk:
        # sample when the clock reaches the first register, apply inputs D_IN after it reached the last
        skew = lat[1] - lat[0]
        off0, w1, w2 = ((D_IN, period - D_IN - FUNC_SETUP, FUNC_SETUP + D_IN) if func
                        else (lat[1] + D_IN, period - D_IN - skew, D_IN + skew))
        L += [f"  initial begin {ident(clk)} = 1'b0; #{GSR_NS:.3f}; forever begin #{half:.3f} {ident(clk)} = 1'b1; "
              f"#{half:.3f} {ident(clk)} = 1'b0; end end", "  initial begin"]
        if rst:
            L.append(f"    {ident(rst)} = {on};")
        L += [f"    in_{safe(n)} = 0;" for n, _ in ins]
        L += [f'    fi = $fopen("{vec}", "r"); fo = $fopen("outputs.hex", "w");',
              f"    #{GSR_NS + half + 4 * period + off0:.3f};"]
        if rst:
            L.append(f"    {ident(rst)} = {off};")
        L += ['    $display("AXS start");', "    while (!$feof(fi)) begin", f"      {rd}",
              f"      if (r == {len(ins)}) begin #{w1:.3f}; {wr} #{w2:.3f}; end", "    end",
              f"    #{w1:.3f}; {wr}"]
    else:
        L += ["  initial begin"] + [f"    in_{safe(n)} = 0;" for n, _ in ins]
        L += [f'    fi = $fopen("{vec}", "r"); fo = $fopen("outputs.hex", "w");', f"    #{GSR_NS:.3f};",
              '    $display("AXS start");', "    while (!$feof(fi)) begin", f"      {rd}",
              f"      if (r == {len(ins)}) begin #{period:.3f}; {wr} end", "    end"]
    L += ['    $display("AXS end");', "    $fclose(fi); $fclose(fo); $finish;", "  end", "endmodule"]
    return "\n".join(L) + "\n"


def sim_one(job: Dict[str, Any]) -> Dict[str, Any]:
    d = Path(job["dir"])
    bin_ = Path(job["vivado"]).parent
    glbl = str(bin_.parent / "data/verilog/src/glbl.v")
    t0, top, P = time.time(), job["top"], job["sim_ns"]
    res: Dict[str, Any] = dict(design=job["design"], variant=job["variant"], ok=False, sim_ns=P)
    to = job["timeout"]
    try:
        # functional reference
        f = d / "fsim"
        f.mkdir(exist_ok=True)
        (f / "tb.v").write_text(tb_text(top, job["ports"], job["vec"], P, [0.0, 0.0], True))
        run([str(bin_ / "xvlog"), "../post_func.v", "tb.v", glbl], f, "xvlog.out", to)
        run([str(bin_ / "xelab"), "-L", "unisims_ver", "-s", "simf", "tb", "glbl", "-timescale", "1ns/1ps"], f,
            "xelab.out", to)
        o = run([str(bin_ / "xsim"), "simf", "-R", "-log", "xsim.log"], f, "xsim.out", to)
        if "AXS end" not in o:
            res["error"] = "functional sim failed: " + o[-300:]
            return res
        # timing simulation with SAIF
        (d / "tb.v").write_text(tb_text(top, job["ports"], job["vec"], P, job["clk_arrival"] or [0.0, 0.0], False))
        run([str(bin_ / "xvlog"), "post.v", "tb.v", glbl], d, "xvlog.out", to)
        # no -pulse_r 0 / -pulse_e 0: xsim then leaves a combinational DSP48E1 output stale
        run([str(bin_ / "xelab"), "-L", "simprims_ver", "-maxdelay", "-transport_int_delays", "-pulse_int_r", "0",
             "-pulse_int_e", "0", "-debug", "typical", "-s", "sim", "tb", "glbl", "-timescale", "1ns/1ps"], d,
            "xelab.out", to)
        (d / "saif.tcl").write_text("open_saif sim.saif\nlog_saif [get_objects -r /tb/dut/*]\nrun all\nclose_saif\nquit\n")
        o = run([str(bin_ / "xsim"), "sim", "-tclbatch", "saif.tcl", "-log", "xsim.log"], d, "xsim.out", to)
        if not (d / "sim.saif").exists() or "AXS end" not in o:
            res["error"] = "timing sim failed: " + o[-300:]
            return res
        tl = (d / "outputs.hex").read_text().splitlines()
        fl = (f / "outputs.hex").read_text().splitlines()
        xlog = (d / "xsim.log").read_text(errors="ignore") if (d / "xsim.log").exists() else o
        res.update(vectors=len(tl), x_lines=sum(1 for ln in tl if "x" in ln.lower()),
                   func_x_lines=sum(1 for ln in fl if "x" in ln.lower()),
                   mismatch_vs_func=(sum(1 for a, b in zip(tl, fl) if a != b) + abs(len(tl) - len(fl))),
                   violations=len(re.findall(r"violation", xlog, re.I)),
                   sdf_annotated=bool(re.search(r"sdf", (d / "xelab.out").read_text(errors="ignore"), re.I)))
        clk = job["ports"]["clk"]
        ck = f"create_clock -name clk -period {P} [get_ports {{{clk}}}]" if clk else f"create_clock -name clk -period {P}"
        (d / "pwr.tcl").write_text(f"open_checkpoint post.dcp\n{ck}\nset_units -power mW\n"
                                   "read_saif -strip_path tb/dut sim.saif\nreport_power -file power_saif.rpt\nquit\n")
        run([job["vivado"], "-mode", "batch", "-nojournal", "-log", "pwr.log", "-source", "pwr.tcl"], d, "pwr.out", to)
        pw = (d / "power_saif.rpt").read_text(errors="ignore") if (d / "power_saif.rpt").exists() else ""
        m = re.search(r"Design Nets Matched\s*\|\s*(\d+)%\s*\((\d+)/(\d+)\)", pw)
        res.update(dyn_mw=num(r"^\|\s*Dynamic \(mW\)\s*\|\s*([\d.]+)", pw),
                   total_mw=num(r"Total On-Chip Power \(mW\)\s*\|\s*([\d.]+)", pw),
                   confidence=(re.search(r"Overall confidence level\s*\|\s*(\w+)", pw) or [None, None])[1],
                   saif_nets_matched_pct=int(m.group(1)) if m else None)
        res["ok"] = res["dyn_mw"] is not None
    except Exception as exc:                                            # keep the batch running
        res["error"] = str(exc)[:300]
    res["sim_s"] = round(time.time() - t0, 1)
    (d / "sim.json").write_text(json.dumps(res, indent=1))
    return res


# ----------------------------------------------------------------------------------------------------- report

def _dec(s: str, w: int, fmt: Optional[str]) -> Optional[int]:
    try:
        u = int(s, 16)
    except ValueError:
        return None
    if fmt and fmt.startswith("sm"):
        m = u & ((1 << (w - 1)) - 1)
        return -m if u >> (w - 1) else m
    if fmt and re.match(r"s\d", fmt):
        return u - (1 << w) if u >> (w - 1) else u
    return u


def error_check(out: Path, d: str, ports: Dict[str, Any], meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Largest exact-vs-approximate difference per relaxed output, from the functional post-route outputs."""
    fe, fa = out / d / "exact/fsim/outputs.hex", out / d / "approx/fsim/outputs.hex"
    if not fe.exists() or not fa.exists():
        return None
    e = [ln.split() for ln in fe.read_text().splitlines()]
    a = [ln.split() for ln in fa.read_text().splitlines()]
    fm = {s["wire"]: (s["quality"].get("format"), s["bound"]) for s in meta.get("relaxed_signals", [])}
    res = {"lines": len(e), "diff_lines": sum(1 for x, y in zip(e, a) if x != y), "outputs": {}, "other_diff": 0}
    for i, (n, w) in enumerate(ports["outputs"]):
        mx = 0
        for x, y in zip(e, a):
            if i >= len(x) or i >= len(y):
                continue
            u, v = _dec(x[i], w, fm.get(n, (None,))[0]), _dec(y[i], w, fm.get(n, (None,))[0])
            if u is not None and v is not None:
                mx = max(mx, abs(u - v))
        if n in fm:
            res["outputs"][n] = {"max_err": mx, "bound": fm[n][1]}
        else:
            res["other_diff"] = max(res["other_diff"], mx)
    return res


def _pct(e: Optional[float], a: Optional[float]) -> Optional[float]:
    return None if e in (None, 0) or a is None else 100.0 * (e - a) / e


def _f(v: Optional[float], nd: int = 1) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def report(out: Path, results: Dict[str, Dict[str, Any]], impl: Dict, sim: Dict, checks: Dict, period: float) -> None:
    designs = sorted({d for d, _ in impl})
    pairs = [d for d in designs if impl.get((d, "exact"), {}).get("ok") and impl.get((d, "approx"), {}).get("ok")]
    tag = lambda d: ("exact rewrite" if results[d].get("exact_rewrite") else "")

    def gate(d: str, v: str) -> List[str]:
        i, s = impl.get((d, v), {}), sim.get((d, v), {})
        bad = []
        if not s.get("ok"):
            return ["no simulation"]
        if i.get("whs") is not None and i["whs"] < 0:
            bad.append(f"WHS {i['whs']:.3f}")
        if s.get("violations"):
            bad.append(f"{s['violations']} timing-check violations")
        if s.get("x_lines"):
            bad.append(f"{s['x_lines']} X lines")
        if s.get("mismatch_vs_func"):
            bad.append(f"{s['mismatch_vs_func']} lines differ from functional")
        return bad

    md = ["# Yosys netlist (EDIF) through Vivado place and route: exact vs approximate", "",
          f"Both variants: the netlist Yosys mapped in the suite run, written as EDIF; Vivado link_design (out of "
          f"context), place_design, phys_opt_design, route_design. No Vivado synthesis, no opt_design. Clock "
          f"{period:g} ns ({1000 / period:.0f} MHz). Saved % = (exact - approx) / exact.", "",
          "LC cells = netlist LUT cells + 2 x CARRY4 (the suite's measure; it follows the Yosys netlist by "
          "construction, since Vivado does not re-map it). LC sites = occupied LUT sites + 2 x CARRY4: Vivado packs "
          "two small LUTs into one site, so this is the area the device actually spends.", "",
          "## Resources after route", "",
          "| Design | LC cells | LC sites | LUT sites | FF | CARRY4 | DSP | BRAM tiles | Slices | note |",
          "|:--|--:|--:|--:|--:|--:|--:|--:|--:|:--|"]
    lc_sv, sl_sv, lcc_sv = {}, {}, {}
    for d in designs:
        e, a = impl.get((d, "exact"), {}), impl.get((d, "approx"), {})
        if not e.get("ok"):
            md.append(f"| {d} | exact run failed: {e.get('error', '')[:80]} | | | | | | | | |")
            continue
        if d not in pairs:
            why = "exact fallback" if results[d]["fell_back"] else "approx run failed: " + a.get("error", "")[:80]
            md.append(f"| {d} | {e['lc_cells']} | {e['lc']} | {e['luts']} | {e['ffs']} | {e['carry4']} | {e['dsps']} | "
                      f"{e['bram_tiles']:g} | {e['slices']} | {why} |")
            continue
        lc_sv[d], sl_sv[d], lcc_sv[d] = _pct(e["lc"], a["lc"]), _pct(e["slices"], a["slices"]), _pct(e["lc_cells"], a["lc_cells"])
        cell = lambda k: f"{e[k]:g} -> {a[k]:g}" + ("" if _pct(e[k], a[k]) is None else f" ({_pct(e[k], a[k]):+.1f} %)")
        md.append(f"| {d} | " + " | ".join(cell(k) for k in ("lc_cells", "lc", "luts", "ffs", "carry4", "dsps", "bram_tiles", "slices"))
                  + f" | {tag(d)} |")
    md.append("")
    drift = []
    for (d, v), i in sorted(impl.items()):
        s = results[d]["exact" if v == "exact" else "approx"] or {}
        if i.get("ok") and s:
            df = [f"{k} {s.get(sk)} -> {i.get(k)}" for k, sk in (("lut_cells", "luts"), ("ffs", "ffs"), ("carry4", "carry4"),
                                                                 ("dsps", "dsps")) if s.get(sk) != i.get(k)]
            if df:
                drift.append(f"- {d} {v}: " + ", ".join(df))
    md += ["Netlist cell counts that differ from the suite's Yosys counts (suite -> after route). phys_opt_design "
           "changes the netlist: it replicates cells and moves registers out of DSP48 blocks into flip-flops.", ""] \
          + (drift or ["- none"]) + [""]

    md += ["## Timing and power", "",
           f"WNS and Fmax from the {period:g} ns implementation (Fmax = 1 / (period - WNS)). Sim period: {period:g} ns "
           "when both variants meet it, otherwise the smallest period at which both pass the simulation checks (STA "
           "period, then 3 % steps: the simulator's CARRY4 delays are slower than STA's). Dynamic power: Vivado report_power "
           "with the SAIF of the post-route timing simulation (SDF max delays, application stream), no I/O power "
           "(out of context). xsim settings: transport delays on interconnect, default pulse handling on cell paths. "
           "A run is valid when hold is met, the simulator logs no timing-check violation, no "
           "output is X, and the timing-simulation outputs equal the functional simulation of the same netlist. "
           "SAIF nets: share of design nets that got their activity from the SAIF (the rest use Vivado's estimate).", "",
           "| Design | WNS ns exact / approx | Fmax MHz exact -> approx | Sim period ns | Vectors | SAIF dyn. mW exact -> approx | Saved % | Vectorless saved % | SAIF nets % exact / approx | Valid |",
           "|:--|--:|--:|--:|--:|--:|--:|--:|--:|:--|"]
    pw_sv, pw_valid, fm_sv = {}, {}, {}
    for d in designs:
        e, a = impl.get((d, "exact"), {}), impl.get((d, "approx"), {})
        se, sa = sim.get((d, "exact"), {}), sim.get((d, "approx"), {})
        if not e.get("ok"):
            continue
        if d not in pairs:
            g = gate(d, "exact")
            md.append(f"| {d} | {_f(e['wns'], 3)} / - | {_f(e['fmax_mhz'])} | {_f(se.get('sim_ns'))} | {se.get('vectors', '-')} | "
                      f"{_f(se.get('dyn_mw'), 3)} | - | - | {se.get('saif_nets_matched_pct', '-')} / - | {'yes' if not g else '; '.join(g)} |")
            continue
        g = [f"exact: {x}" for x in gate(d, "exact")] + [f"approx: {x}" for x in gate(d, "approx")]
        sv = _pct(se.get("dyn_mw"), sa.get("dyn_mw"))
        vl = _pct(e.get("vectorless_dyn_mw"), a.get("vectorless_dyn_mw"))
        fm_sv[d] = -_pct(e["fmax_mhz"], a["fmax_mhz"])
        if sv is not None:
            pw_sv[d], pw_valid[d] = sv, not g
        md.append(f"| {d} | {_f(e['wns'], 3)} / {_f(a['wns'], 3)} | {_f(e['fmax_mhz'])} -> {_f(a['fmax_mhz'])} ({fm_sv[d]:+.1f} %) | "
                  f"{_f(se.get('sim_ns'))} | {se.get('vectors', '-')} | {_f(se.get('dyn_mw'), 3)} -> {_f(sa.get('dyn_mw'), 3)} | "
                  f"{'-' if sv is None else f'{sv:+.1f}'} | {'-' if vl is None else f'{vl:+.1f}'} | "
                  f"{se.get('saif_nets_matched_pct', '-')} / {sa.get('saif_nets_matched_pct', '-')} | {'yes' if not g else '; '.join(g)} |")
    md.append("")

    md += ["## Error of the implemented netlists", "",
           "Largest |exact - approximate| per relaxed output over the application stream, from the functional "
           "simulation of the two post-route netlists, in the output's number format.", "",
           "| Design | Vectors | Lines that differ | Max error (bound) | Suite WCE | Within bound |", "|:--|--:|--:|:--|--:|:--|"]
    for d in pairs:
        c = checks.get(d)
        if not c:
            md.append(f"| {d} | - | - | - | - | not simulated |")
            continue
        ok = all(o["max_err"] <= o["bound"] for o in c["outputs"].values()) and not c["other_diff"]
        note = "yes" if ok else ("NO" + (f" (non-relaxed output differs by {c['other_diff']})" if c["other_diff"] else ""))
        md.append(f"| {d} | {c['lines']} | {c['diff_lines']} | "
                  + ", ".join(f"{n}: {o['max_err']} ({o['bound']:g})" for n, o in c["outputs"].items())
                  + f" | {results[d]['errors'].get('wce')} | {note} |")
    md.append("")

    adders = {d for d in pairs if d.startswith(("brent_kung", "kogge_stone"))}
    rew = {d for d in pairs if results[d].get("exact_rewrite")}
    relaxed = {d for d in pairs if (sim.get((d, "exact"), {}).get("sim_ns") or period) > period + 1e-9}

    def line(label: str, vals: Dict[str, float], sel: List[str]) -> str:
        v = [vals[d] for d in sel if d in vals]
        return f"| {label} | {len(v)} | " + ("- | - |" if not v else f"{statistics.mean(v):+.1f} % | {statistics.median(v):+.1f} % |")

    md += ["## Summary", "", "| Quantity | Designs | Mean | Median |", "|:--|--:|--:|--:|",
           line("LC cells saved, all approximated designs", lcc_sv, pairs),
           line("LC cells saved, without the prefix adders", lcc_sv, [d for d in pairs if d not in adders]),
           line("LC cells saved, without prefix adders and exact rewrites", lcc_sv, [d for d in pairs if d not in adders | rew]),
           line("LC sites saved, all approximated designs", lc_sv, pairs),
           line("LC sites saved, without the prefix adders", lc_sv, [d for d in pairs if d not in adders]),
           line("LC sites saved, without prefix adders and exact rewrites", lc_sv, [d for d in pairs if d not in adders | rew]),
           line("Slices saved, all approximated designs", sl_sv, pairs),
           line("Fmax change, all approximated designs", fm_sv, pairs),
           line("SAIF dynamic power saved, valid runs", pw_sv, [d for d in pairs if pw_valid.get(d)]),
           line(f"SAIF dynamic power saved, valid runs at {period:g} ns", pw_sv,
                [d for d in pairs if pw_valid.get(d) and d not in relaxed]),
           line("SAIF dynamic power saved, valid runs, without the prefix adders", pw_sv,
                [d for d in pairs if pw_valid.get(d) and d not in adders]),
           "", f"Prefix adders: {', '.join(sorted(adders)) or '-'}. Exact rewrites (zero error): {', '.join(sorted(rew)) or '-'}. "
               f"Simulated at a relaxed period: {', '.join(sorted(relaxed)) or '-'}. "
               f"Exact fallback (no approximate variant): {', '.join(d for d in designs if results[d]['fell_back']) or '-'}.", ""]
    failed = [(k, v) for k, v in sorted(impl.items()) if not v.get("ok")] + [(k, v) for k, v in sorted(sim.items()) if not v.get("ok")]
    if failed:
        md += ["## Failed runs", ""] + [f"- {d} {v}: {r.get('error', '')[:300]}" for (d, v), r in failed] + [""]
    (out / "VIVADO_EDIF_REPORT.md").write_text("\n".join(md))
    (out / "vivado_edif_results.json").write_text(json.dumps(
        {"clk_ns": period, "impl": [v for _, v in sorted(impl.items())], "sim": [v for _, v in sorted(sim.items())],
         "error_check": checks, "lc_cells_saved_pct": lcc_sv, "lc_sites_saved_pct": lc_sv, "saif_power_saved_pct": pw_sv, "saif_valid": pw_valid}, indent=1))


# ------------------------------------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-build", required=True)
    ap.add_argument("--run-results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--designs", default="")
    ap.add_argument("--clk-ns", type=float, default=10.0)
    ap.add_argument("--stimulus", choices=("app", "power"), default="app")
    ap.add_argument("--vivado", default=os.environ.get("AXFPGA_VIVADO", "vivado"))
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=4 * 3600)
    ap.add_argument("--max-period-steps", type=int, default=8, help="3 %% period increases tried when a simulation fails its checks")
    ap.add_argument("--force", action="store_true", help="redo runs that already have results")
    ap.add_argument("--redo-sim", action="store_true", help="keep the implementations, redo every simulation")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    from axfpga.pipeline import _top_ports, _with_meta_ports
    import run_pyals_yosys_suite as suite
    benches = {b["id"]: b for b in suite.define_benchmark_suite()}
    results = {r["id"]: r for r in json.loads(Path(a.run_results).read_text())}
    ids = [d for d in (a.designs.split(",") if a.designs else sorted(results)) if d]
    info: Dict[str, Dict[str, Any]] = {}
    for d in ids:
        r, b, bdir = results[d], benches[d], Path(a.run_build).resolve() / d
        ports = _top_ports(b["files"], r["top"], b.get("clock"), b.get("reset"), b.get("reset_active_low"))
        meta = json.loads((bdir / "cone_meta.json").read_text()) if (bdir / "cone_meta.json").exists() else {}
        ports = _with_meta_ports(ports, meta, b.get("clock"), b.get("reset"), b.get("reset_active_low"))
        vec = bdir / ("app/app_exact_vectors.hex" if a.stimulus == "app" else "power/pwr_exact_vectors.hex")
        if not vec.exists():
            vec = bdir / "power/pwr_exact_vectors.hex"
        variants = [v for v, fn in VARIANTS.items() if (bdir / fn).exists() and not (v == "approx" and r["fell_back"])]
        info[d] = dict(r=r, bdir=bdir, ports=ports, meta=meta, vec=str(vec), variants=variants)

    def load(name: str) -> Dict:
        res = {}
        for d in ids:
            for v in info[d]["variants"]:
                p = out / d / v / name
                if p.exists():
                    res[(d, v)] = json.loads(p.read_text())
        return res

    impl = {} if a.force else {k: v for k, v in load("impl.json").items() if v.get("ok") and v.get("clk_ns") == a.clk_ns}
    if not a.report_only:
        jobs = [dict(design=d, variant=v, netlist=str(info[d]["bdir"] / VARIANTS[v]), top=info[d]["r"]["top"],
                     part=info[d]["r"]["device"], clk=info[d]["ports"]["clk"], period=a.clk_ns, dir=str(out / d / v),
                     vivado=a.vivado, threads=a.threads, timeout=a.timeout)
                for d in ids for v in info[d]["variants"] if (d, v) not in impl]
        jobs.sort(key=lambda j: -Path(j["netlist"]).stat().st_size)
        print(f"{len(jobs)} implementation run(s), {len(impl)} already done", flush=True)
        with ProcessPoolExecutor(max_workers=a.parallel) as pool:
            for f in as_completed([pool.submit(impl_one, j) for j in jobs]):
                r = f.result()
                impl[(r["design"], r["variant"])] = r
                print(f"impl {r['design']:20} {r['variant']:7} {'ok ' if r['ok'] else 'FAIL'} LC {r.get('lc')} WNS {r.get('wns')} "
                      f"WHS {r.get('whs')} clk {r.get('clk_arrival')} {r.get('impl_s')}s {r.get('error', '')[:150]}", flush=True)
    sim = {} if a.force or a.redo_sim else {k: v for k, v in load("sim.json").items() if v.get("ok")}
    if not a.report_only:
        sim_clean = lambda s: bool(s.get("ok")) and not (s.get("violations") or s.get("x_lines") or s.get("mismatch_vs_func"))
        period: Dict[str, float] = {}
        for d in ids:
            vs = [v for v in info[d]["variants"] if impl.get((d, v), {}).get("ok")]
            if not vs:
                continue
            worst = min(impl[(d, v)]["wns"] for v in vs)
            period[d] = a.clk_ns if worst >= 0 else math.ceil((a.clk_ns - worst) * 10 - 1e-9) / 10
            done = [sim[(d, v)]["sim_ns"] for v in vs if (d, v) in sim]
            if len(done) == len(vs) and len(set(done)) == 1 and done[0] >= period[d] - 1e-9:
                period[d] = done[0]                      # a finished pair, possibly at an already raised period
        def raise_failed() -> None:
            for d in list(period):
                vs = [v for v in info[d]["variants"] if impl.get((d, v), {}).get("ok")]
                if any((d, v) in sim and abs(sim[(d, v)]["sim_ns"] - period[d]) < 1e-9 and not sim_clean(sim[(d, v)]) for v in vs):
                    period[d] = math.ceil(period[d] * 1.03 * 10 - 1e-9) / 10

        raise_failed()                                   # pairs loaded from an earlier run that failed the checks
        for attempt in range(a.max_period_steps + 1):
            jobs = []
            for d, p_sim in period.items():
                for v in [v for v in info[d]["variants"] if impl.get((d, v), {}).get("ok")]:
                    if (d, v) in sim and abs(sim[(d, v)].get("sim_ns", 0) - p_sim) < 1e-9:
                        continue
                    sim.pop((d, v), None)
                    jobs.append(dict(design=d, variant=v, top=info[d]["r"]["top"], ports=info[d]["ports"],
                                     vec=info[d]["vec"], sim_ns=p_sim, clk_arrival=impl[(d, v)].get("clk_arrival"),
                                     dir=str(out / d / v), vivado=a.vivado, timeout=a.timeout))
            if not jobs:
                break
            jobs.sort(key=lambda j: -(Path(j["dir"]) / "post.v").stat().st_size)
            print(f"{len(jobs)} simulation run(s), attempt {attempt}", flush=True)
            with ProcessPoolExecutor(max_workers=a.parallel) as pool:
                for f in as_completed([pool.submit(sim_one, j) for j in jobs]):
                    r = f.result()
                    sim[(r["design"], r["variant"])] = r
                    print(f"sim  {r['design']:20} {r['variant']:7} {'ok ' if r['ok'] else 'FAIL'} P {r.get('sim_ns')} dyn {r.get('dyn_mw')} mW "
                          f"viol {r.get('violations')} x {r.get('x_lines')} !=func {r.get('mismatch_vs_func')} {r.get('sim_s')}s "
                          f"{r.get('error', '')[:150]}", flush=True)
            # xsim carry delays are slower than STA: lengthen the period of a failing design, both variants
            if attempt < a.max_period_steps:
                raise_failed()
    checks = {d: c for d in ids if (c := error_check(out, d, info[d]["ports"], info[d]["meta"]))}
    report(out, results, impl, sim, checks, a.clk_ns)
    print("wrote", out / "VIVADO_EDIF_REPORT.md")


if __name__ == "__main__":
    main()
