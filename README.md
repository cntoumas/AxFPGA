# AxFPGA

Attribute-driven approximate computing for FPGAs, starting from standard Verilog.

## Motivation

Approximate computing trades a bounded output error for less area and power. No commercial FPGA
compiler accepts approximation directives, and most research tools work on their own netlist formats
or need a modified compiler.

AxFPGA needs neither. The designer marks, in plain Verilog, which signals may be approximated and by
how much. The marks are IEEE 1364 attributes (`(* ... *)`), so the source stays legal Verilog for every
tool. The open-source Yosys suite reads the attributes, and AxFPGA uses them to find, approximate and
check the logic.

## How it works

1. **Annotate.** Attributes on wires, regs or ports state what is relaxed and the error allowed.
2. **Extract.** Yosys elaborates the design. AxFPGA extracts the *Cone*: the combinational logic that
   drives relaxed signals and no precise signal.
3. **Approximate.** A library of techniques proposes approximate versions of the Cone:
   - pre-characterized approximate 4-LUTs from the pyALS catalog, LUT pruning, output-bit pruning
   - truncation of arithmetic operands, internal wires and outputs, with error compensation
   - lower-part OR adders (LOA) and carry cut at 4-bit boundaries
   - partial-product pruning of multipliers
   - truncation of arithmetic behind internal registers
4. **Explore.** Technique, site and depth form a design space. A metaheuristic search (tabu search by
   default, others selectable) looks for the cheapest design that meets the error bound. Cost is
   counted in FPGA Logic Cells (`LUT + 2 x CARRY4`, DSP slices weighted) after `synth_xilinx`.
5. **Verify.** The chosen design is put back into the full design and simulated against the exact one
   on the application stimulus and on a held-out stimulus. SAT-based checks (precise outputs unchanged,
   worst-case error within the bound) are available in `axfpga/verifier.py`.
6. **Report.** Utilization, activity-based power estimate, error statistics and the accepted netlist.

## Annotation example

```verilog
module mac (
    input                    clk,
    (* axc_precise = "1" *)  input  enable,
    (* axc_precise = "1" *)  input  clear,
    input  [7:0]             a, b,
    (* axc_relax = "1", axc_metric = "WCE", axc_bound = "64" *)
    output reg [19:0]        acc_out
);
```

| Attribute | Meaning |
|---|---|
| `axc_relax` | the signal may be approximated |
| `axc_precise` | the signal must stay exact |
| `axc_bound` | maximum error allowed on the relaxed signal |
| `axc_metric` | error measure the bound applies to: `WCE`, `MED` or `ER` |

## Install

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Icarus Verilog (`iverilog`, `vvp`).
Yosys 0.70 comes with the `pyosys` wheel; no system Yosys is needed.

```bash
git clone --recurse-submodules <repo> AxFPGA && cd AxFPGA
uv sync --extra dev
.venv/bin/axfpga-yosys -V
.venv/bin/python -m pytest tests
```

## Run

```bash
# one design
axfpga -i benchmarks/mac/mac.v -o build/mac_run

# benchmark suite (AxBench and in-house designs) for the Zybo Z7-10 (xc7z010)
scripts/run_axbench_zybo.sh

# Vivado check of a suite run: exact and approximate netlists, placed and routed the same way
python scripts/vivado_edif.py --run-build build/<run> --run-results reports/results.json --out reports/vivado_edif
```

`vivado_edif.py` writes both Yosys netlists as EDIF and runs `link_design`, `place_design`,
`phys_opt_design` and `route_design` (no Vivado synthesis) with a 10 ns clock. Power comes from the SAIF
of a post-route timing simulation on the application stimulus.

Settings are read from environment variables (`axfpga/config.py`): `AXFPGA_AXBENCH_ROOT` (AxBench
checkout), `AXFPGA_YOSYS` (Yosys command), `AXFPGA_VIVADO` (Vivado executable, for comparison runs).

## Status

Working: annotation, Cone extraction, the technique library, the search, simulation-based checking and
reporting, on 23 benchmark designs.

In progress:

- one joint search over technique combinations across sites, instead of one search per technique
- multi-objective search (area, power, logic depth) returning a Pareto front
- formal proof of the selected design as part of the default flow

Planned: Vivado implementation inside the flow, and a single command for the whole run.

Cost and power numbers of the flow come from Yosys synthesis; `scripts/vivado_edif.py` checks them
with Vivado.

## Acknowledgement

The LUT approximation catalog and parts of the candidate generation build on
[pyALS](https://github.com/SalvatoreBarone/pyALS) by Salvatore Barone.
