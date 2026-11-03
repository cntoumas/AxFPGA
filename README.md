# AxFPGA: Attribute-Driven Approximate Logic Synthesis for FPGAs

AxFPGA is a native FPGA Approximate Logic Synthesis (ALS) & Formal Verification framework targeting modern SRAM-based FPGA architectures, specifically **Xilinx 7-Series / Artix-7 (`xc7a35t`)**.

---

## Key Features

1. **Approximation Engine: pyALS (Salvatore Barone)**:
   - **`pyALS` / `pyALSlib` / `pyALS-lut-catalog`**:
     - Catalog-based AIG-rewriting Approximate Logic Synthesis.
     - Maps circuits to K-LUT graphs, queries pre-characterized 4-LUT approximation database (`lut_catalog.db`), and applies multi-objective rewriting under error bounds.
     - Performs distance-k truth table rewrites, cumulative LUT pruning, and output bit-level simplifications.

2. **Physical Architecture Modeling**:
   - Explicit Xilinx 7-Series primitive decomposition: LUT6, LUT5..1, CARRY4, FDRE/FDSE, DSP48E1, BRAM.
   - Normalized metric: **Logic Cells** ($LC = \text{LUT} + 2 \times \text{CARRY4}$).
   - Multi-Algorithm Tournament selecting champion mutant that minimizes physical Logic Cells while strictly satisfying user error bounds (MED, WCE, ER).

3. **Formal SAT Verification**:
   - 100% Bit-exact sequential control equivalence proving using Yosys SAT engines (`sat -verify -tempinduct`).
   - Dual-miter cycle-accurate simulation under realistic or trace-driven stimulation.

---

## Installation & Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Icarus Verilog (`iverilog`, `vvp`) on `PATH`.
Yosys is pinned to **0.70** through the `pyosys` wheel; no system Yosys is used. The wheel has no
`yosys` executable, so AxFPGA ships `axfpga-yosys` (`python -m axfpga.yosys_cli`) on top of the same `libyosys`.

```bash
git clone --recurse-submodules <repo> AxFPGA && cd AxFPGA

# Project-local venv in .venv with axfpga and the local pyALSlib submodule (editable)
uv sync --extra dev
.venv/bin/axfpga-yosys -V        # Yosys 0.70
.venv/bin/python -m pytest tests
```

Configuration (environment variables, see `axfpga/config.py`): `AXFPGA_AXBENCH_ROOT` (AxBench checkout,
default `~/Downloads/axbench-verilog`), `AXFPGA_VIVADO` (Vivado executable, validation only),
`AXFPGA_YOSYS` (override the Yosys command line).

---

## CLI Usage

```bash
# Full automated end-to-end FPGA synthesis flow
axfpga -i benchmarks/mac/mac.v -o build/mac_run

# AxBench evaluation on the Zybo Z7-10 (xc7z010), parallel; results in reports/
scripts/run_axbench_zybo.sh

# Automated Pareto sweeps over error bounds
axfpga-sweep -i benchmarks/mac/mac.v -o reports/mac_sweep -b "8, 16, 32, 64, 128"
```
