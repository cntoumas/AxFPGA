"""Activity-based dynamic power estimation for Yosys-mapped Xilinx 7-Series netlists.

Method:
1. Simulate the Yosys-mapped netlist (LUT/CARRY4/FD*/DSP48E1/RAMB* primitives, Yosys
   cells_sim.v models) in iverilog with a given per-cycle input vector stream.
2. Count output-bit toggles of every mapped cell from the VCD (zero-delay simulation,
   so glitch power is not captured).
3. P_dyn = sum over cells of E_toggle(type) * toggle_rate * f_clk, plus FF clock load
   and per-active-cycle energy for DSP48E1 / block RAM.

The per-primitive energies are fixed assumptions for 7-Series at VCCINT = 1.0 V
(ENERGY_PJ below). Static power is not modelled: it is a device constant, identical
for any two designs on the same part. Results are estimates, not sign-off power.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from axfpga import cache
from axfpga.config import xilinx_cells_sim

logger = logging.getLogger("axfpga.activity_power")

XILINX_CELLS_SIM = xilinx_cells_sim()   # pinned Yosys share (pyosys wheel)
CYCLE_NS = 10

# Energy assumptions (pJ), 7-Series @ VCCINT = 1.0 V, including local + general routing of the driven net.
ENERGY_PJ: Dict[str, float] = {
    "LUT": 0.40,        # per output-bit toggle (LUT1..6, LUT6_2 O5/O6, INV, LUTRAM/SRL outputs)
    "MUXF": 0.20,       # per output toggle (MUXF7/MUXF8)
    "CARRY4": 0.20,     # per output-bit toggle (O[3:0], CO[3:0])
    "FF": 0.30,         # per Q toggle
    "FF_CLOCK": 0.02,   # per flip-flop per clock cycle (clock-tree load)
    "DSP48E1": 12.0,    # per active cycle (any input bit toggled)
    "RAMB18E1": 5.0,    # per active cycle
    "RAMB36E1": 10.0,   # per active cycle
}


def cell_class(cell_type: str) -> str:
    t = cell_type.lstrip("\\")
    if t.startswith(("BUFG", "IBUF", "OBUF", "BUFH")):
        return "CLOCK_IO"  # not modelled: clock/IO buffers are identical across baseline and approximation
    if t.startswith("FD"):
        return "FF"
    if t == "CARRY4":
        return "CARRY4"
    if t.startswith("MUXF"):
        return "MUXF"
    if t in ("DSP48E1", "RAMB18E1", "RAMB36E1"):
        return t
    return "LUT"


def _fmt_ident(name: str) -> str:
    return f"\\{name} " if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) else name


def run_vector_sim(
    sources: Sequence[str | Path],
    top: str,
    inputs: List[Tuple[str, int]],
    outputs: List[Tuple[str, int]],
    vectors: List[Dict[str, int]],
    work_dir: str | Path,
    clk: Optional[str] = None,
    rst: Optional[str] = None,
    rst_active_low: bool = False,
    reset_cycles: int = 4,
    vcd: bool = False,
    tag: str = "sim",
    timeout: int = 3600,
) -> Dict[str, Any]:
    """Drive `top` with one input vector per cycle; return per-cycle output values (and VCD path).

    inputs/outputs: (port_name, width) excluding clk/rst. Unknown output bits read as 0.
    """
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    vec_file = work_dir / f"{tag}_vectors.hex"
    out_file = work_dir / f"{tag}_outputs.hex"
    vcd_file = work_dir / f"{tag}.vcd"
    tb_file = work_dir / f"{tag}_tb.v"
    vvp_file = work_dir / f"{tag}.vvp"

    vec_text = "".join(" ".join(f"{v.get(n, 0) & ((1 << w) - 1):x}" for n, w in inputs) + "\n" for v in vectors)
    vec_file.write_text(vec_text)
    ckey = cache.make_key("vsim", [str(s) for s in sources], top=top, inputs=inputs, outputs=outputs,
                          vectors=hashlib.sha256(vec_text.encode()).hexdigest(), clk=clk, rst=rst,
                          rst_active_low=rst_active_low, reset_cycles=reset_cycles, vcd=vcd)
    hit = cache.load("vsim", ckey)
    if hit is not None and (not vcd or cache.restore(hit[1], "sim.vcd", vcd_file)):
        return {"outputs": [dict(r) for r in hit[0]["outputs"]], "vcd": vcd_file if vcd else None, "cycles": hit[0]["cycles"]}

    L: List[str] = ["`timescale 1ns/1ps", "module tb;"]
    if clk:
        L.append(f"  reg {_fmt_ident(clk)};")
    if rst:
        L.append(f"  reg {_fmt_ident(rst)};")
    for n, w in inputs:
        L.append(f"  reg [{w - 1}:0] in_{re.sub(r'[^A-Za-z0-9_]', '_', n)};")
    for n, w in outputs:
        L.append(f"  wire [{w - 1}:0] out_{re.sub(r'[^A-Za-z0-9_]', '_', n)};")
    conns = []
    if clk:
        conns.append(f".{_fmt_ident(clk)}({_fmt_ident(clk)})")
    if rst:
        conns.append(f".{_fmt_ident(rst)}({_fmt_ident(rst)})")
    conns += [f".{_fmt_ident(n)}(in_{re.sub(r'[^A-Za-z0-9_]', '_', n)})" for n, _ in inputs]
    conns += [f".{_fmt_ident(n)}(out_{re.sub(r'[^A-Za-z0-9_]', '_', n)})" for n, _ in outputs]
    L.append(f"  {top} dut ({', '.join(conns)});")
    in_names = [f"in_{re.sub(r'[^A-Za-z0-9_]', '_', n)}" for n, _ in inputs]
    out_names = [f"out_{re.sub(r'[^A-Za-z0-9_]', '_', n)}" for n, _ in outputs]
    rst_on, rst_off = ("1'b0", "1'b1") if rst_active_low else ("1'b1", "1'b0")
    half = CYCLE_NS // 2
    L += ["  integer fi, fo, r;", "  initial begin"]
    if clk:
        L.append(f"    {_fmt_ident(clk)} = 1'b0;")
    if rst:
        L.append(f"    {_fmt_ident(rst)} = {rst_on};")
    for n in in_names:
        L.append(f"    {n} = 0;")
    L += [f'    fi = $fopen("{vec_file}", "r");', f'    fo = $fopen("{out_file}", "w");']
    if clk:
        L.append(f"    repeat ({reset_cycles}) begin #{half} {_fmt_ident(clk)} = 1'b1; #{half} {_fmt_ident(clk)} = 1'b0; end")
    else:
        L.append(f"    #{CYCLE_NS};")
    if rst:
        L.append(f"    {_fmt_ident(rst)} = {rst_off};")
    if vcd:
        L += [f'    $dumpfile("{vcd_file}");', "    $dumpvars(1, dut);"]
    fmt = " ".join(["%h"] * len(inputs))
    L += ["    while (!$feof(fi)) begin", f'      r = $fscanf(fi, "{fmt}\\n", {", ".join(in_names)});', f"      if (r == {len(inputs)}) begin"]
    if clk:
        L.append(f"        #{half} {_fmt_ident(clk)} = 1'b1; #{half} {_fmt_ident(clk)} = 1'b0;")
    else:
        L.append(f"        #{CYCLE_NS};")
    ofmt = " ".join(["%h"] * len(outputs))
    L += [f'        $fwrite(fo, "{ofmt}\\n", {", ".join(out_names)});', "      end", "    end",
          "    $fclose(fi); $fclose(fo);", "    $finish;", "  end", "endmodule"]
    tb_file.write_text("\n".join(L) + "\n")

    libs = [str(XILINX_CELLS_SIM)] if XILINX_CELLS_SIM.exists() else []
    cmd = ["iverilog", "-g2012", "-o", str(vvp_file), "-s", "tb", str(tb_file)] + [str(s) for s in sources] + libs
    comp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if comp.returncode != 0:
        raise RuntimeError(f"iverilog compile failed for {top}: {comp.stderr[-2000:]}")
    run = subprocess.run(["vvp", "-n", str(vvp_file)], capture_output=True, text=True, timeout=timeout, cwd=work_dir)
    if run.returncode != 0:
        raise RuntimeError(f"vvp failed for {top}: {run.stderr[-2000:]}")

    rows: List[Dict[str, int]] = []
    for line in out_file.read_text().splitlines():
        parts = line.split()
        if len(parts) != len(outputs):
            continue
        rows.append({n: int(re.sub(r"[xXzZ]", "0", p), 16) for (n, _), p in zip(outputs, parts)})
    cache.save("vsim", ckey, {"outputs": rows, "cycles": len(rows)}, {"sim.vcd": vcd_file} if vcd else None)
    return {"outputs": rows, "vcd": vcd_file if vcd else None, "cycles": len(rows)}


def _netname_bits(module: Dict[str, Any]) -> Dict[Tuple[str, int], Any]:
    """(net name, wire index) -> json bit id."""
    m: Dict[Tuple[str, int], Any] = {}
    for name, info in module.get("netnames", {}).items():
        off = info.get("offset", 0)
        upto = info.get("upto", 0)
        bits = info["bits"]
        n = len(bits)
        for i, b in enumerate(bits):
            idx = off + (n - 1 - i if upto else i)
            m[(name.lstrip("\\"), idx)] = b
    return m


def _parse_vcd(vcd: Path, name_bits: Dict[Tuple[str, int], Any], watch: Set[Any]) -> Tuple[Dict[Any, int], Dict[Any, Set[int]]]:
    """Toggle count per json bit id; and the set of cycles in which each watched bit toggled."""
    code_map: Dict[str, List[Tuple[int, Any]]] = {}  # vcd code -> [(position in value string, bit id)]
    widths: Dict[str, int] = {}
    toggles: Dict[Any, int] = defaultdict(int)
    active: Dict[Any, Set[int]] = defaultdict(set)
    last: Dict[str, str] = {}
    assigned: Set[Any] = set()  # each json bit counted through one VCD variable only (aliases)
    depth = 0
    t = 0
    t0: Optional[int] = None
    ps_per_unit = 1000  # iverilog default for `timescale 1ns/1ps
    in_timescale = False
    var_re = re.compile(r"\$var\s+\S+\s+(\d+)\s+(\S+)\s+(.+?)\s*(\[(\d+)(?::(\d+))?\])?\s*\$end")
    with open(vcd) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if in_timescale or line.startswith("$timescale"):
                tm = re.search(r"(\d+)\s*(s|ms|us|ns|ps|fs)", line)
                if tm:
                    unit = {"s": 10**12, "ms": 10**9, "us": 10**6, "ns": 1000, "ps": 1, "fs": 0.001}[tm.group(2)]
                    ps_per_unit = int(tm.group(1)) * unit
                in_timescale = "$end" not in line
                continue
            if line.startswith("$scope"):
                depth += 1
                continue
            if line.startswith("$upscope"):
                depth -= 1
                continue
            if line.startswith("$var"):
                mm = var_re.match(line)
                if not mm or depth != 2:  # tb -> dut
                    continue
                w, code, name = int(mm.group(1)), mm.group(2), mm.group(3).strip().lstrip("\\").strip()
                msb = int(mm.group(5)) if mm.group(5) is not None else (w - 1)
                lsb = int(mm.group(6)) if mm.group(6) is not None else (msb if mm.group(5) is not None and w == 1 else 0)
                step = -1 if msb >= lsb else 1
                pos_bits = []
                for j in range(w):
                    b = name_bits.get((name, msb + step * j))
                    if b is not None and not isinstance(b, str) and b not in assigned:
                        pos_bits.append((j, b))
                        assigned.add(b)
                if pos_bits and code not in code_map:
                    code_map[code] = pos_bits
                    widths[code] = w
                continue
            if line[0] == "#":
                t = int(line[1:])
                if t0 is None:
                    t0 = t
                continue
            if line[0] == "$":
                continue
            if line[0] in "bB":
                val, code = line[1:].split()
            elif line[0] in "01xXzZ":
                val, code = line[0], line[1:]
            else:
                continue
            if code not in code_map:
                continue
            w = widths[code]
            val = val.rjust(w, "0" if val[0] in "01" else val[0])[-w:]
            prev = last.get(code)
            last[code] = val
            if prev is None:
                continue
            cyc = int((t - (t0 or 0)) * ps_per_unit // (CYCLE_NS * 1000))
            for j, b in code_map[code]:
                a, c = prev[j], val[j]
                if a != c and a in "01" and c in "01":
                    toggles[b] += 1
                    if b in watch:
                        active[b].add(cyc)
    return toggles, active


def estimate_dynamic_power(
    mapped_json: str | Path,
    top: str,
    vcd: str | Path,
    cycles: int,
    f_clk_mhz: float = 125.0,
) -> Dict[str, Any]:
    """Estimate PL dynamic power (mW) of a mapped netlist from its simulation VCD."""
    data = json.loads(Path(mapped_json).read_text())
    mod = data["modules"].get(top) or next(iter(data["modules"].values()))
    name_bits = _netname_bits(mod)

    cells = mod.get("cells", {})
    watch: Set[Any] = set()
    for c in cells.values():
        if cell_class(c["type"]) in ("DSP48E1", "RAMB18E1", "RAMB36E1"):
            for p, d in c.get("port_directions", {}).items():
                if d == "input" and not p.upper().startswith("CLK"):
                    watch.update(b for b in c["connections"].get(p, []) if not isinstance(b, str))

    toggles, active = _parse_vcd(Path(vcd), name_bits, watch)
    cycles = max(1, cycles)
    scale = f_clk_mhz * 1e-3 / cycles  # pJ * events -> mW

    per_class: Dict[str, Dict[str, float]] = defaultdict(lambda: {"cells": 0, "toggles": 0, "active_cycles": 0, "power_mw": 0.0})
    for c in cells.values():
        cls = cell_class(c["type"])
        if cls == "CLOCK_IO":
            continue
        rec = per_class[cls]
        rec["cells"] += 1
        if cls in ("DSP48E1", "RAMB18E1", "RAMB36E1"):
            cyc: Set[int] = set()
            for p, d in c.get("port_directions", {}).items():
                if d == "input" and not p.upper().startswith("CLK"):
                    for b in c["connections"].get(p, []):
                        cyc |= active.get(b, set())
            rec["active_cycles"] += len(cyc)
            rec["power_mw"] += ENERGY_PJ[cls] * len(cyc) * scale
            continue
        tg = 0
        for p, d in c.get("port_directions", {}).items():
            if d == "output":
                tg += sum(toggles.get(b, 0) for b in c["connections"].get(p, []) if not isinstance(b, str))
        rec["toggles"] += tg
        rec["power_mw"] += ENERGY_PJ[cls] * tg * scale
        if cls == "FF":
            rec["power_mw"] += ENERGY_PJ["FF_CLOCK"] * f_clk_mhz * 1e-3

    breakdown = {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in per_class.items()}
    total = round(sum(v["power_mw"] for v in per_class.values()), 4)
    return {
        "method": "activity-based (zero-delay iverilog VCD of Yosys-mapped netlist)",
        "f_clk_mhz": f_clk_mhz,
        "cycles": cycles,
        "dynamic_power_mw": total,
        "breakdown": breakdown,
        "energy_pj": dict(ENERGY_PJ),
    }
