#!/bin/bash
#SBATCH --job-name=axfpga_axbench
#SBATCH --partition=batch
#SBATCH --nodes=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=12:00:00
#SBATCH -o slurm-%j.out
#SBATCH -e slurm-%j.err
# Full AxBench suite (incl. sobel_npu, excluded locally for memory) + all report figures.
# Needs iverilog on PATH: export IVERILOG_BIN=<dir containing iverilog and vvp> before sbatch.
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
export PATH="${IVERILOG_BIN:?set IVERILOG_BIN}:$HOME/.local/bin:$PATH"
export AXFPGA_AXBENCH_ROOT="$HOME/axbench-verilog"
export PYTHONHASHSEED=0
PY=.venv/bin/python
OUT=reports_cluster_$SLURM_JOB_ID
BUILD=build/cluster_$SLURM_JOB_ID
mkdir -p "$OUT/figures"
iverilog -V | head -1

# sobel_npu is in EXCLUDED; name it explicitly together with every other benchmark id.
IDS=$($PY - <<'E'
import sys; sys.path.insert(0, "scripts")
import run_pyals_yosys_suite as s
print(" ".join(f"-b {b['id']}" for b in s.define_benchmark_suite()))
E
)
# shellcheck disable=SC2086
$PY scripts/run_pyals_yosys_suite.py --out-dir "$OUT" --build-dir "$BUILD" --jobs 4 --inner-jobs 4 --mem-gb 56 $IDS
$PY scripts/generate_pyals_scienceplots.py --out-dir "$OUT"
$PY scripts/generate_report_figures.py --out-dir "$OUT"
echo "DONE $OUT"
