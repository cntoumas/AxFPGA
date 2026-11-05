#!/usr/bin/env bash
# AxBench evaluation on Zybo Z7-10 (xc7z010clg400-1), Yosys only.
# 1. Empties reports/ (all previous reports and figures are removed).
# 2. Runs the suite designs (AxBench and Phase 4, all except EXCLUDED in run_pyals_yosys_suite.py; --group axbench
#    or --group phase4 selects one set): Yosys synthesis, pyALS Tournament, miter + formal SAT, application metrics,
#    activity-based power. --dsp both writes reports/dsp_on, reports/dsp_off and reports/dsp_comparison.md
#    (step 3 then finds no reports/results.json; render the figures per subdirectory).
#    Parallel by default: JOBS benchmarks at once, INNER_JOBS evaluations each, MEM_GB memory budget.
#    Extra arguments go to run_pyals_yosys_suite.py (e.g. -b fir_filter, --jobs 1).
# 3. Renders figures into reports/figures/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"          # project venv (uv sync --extra dev)
export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"   # results do not depend on it; fixed for reproducible logs
JOBS="${JOBS:-6}"
INNER_JOBS="${INNER_JOBS:-2}"
MEM_GB="${MEM_GB:-10}"
REPORTS="$ROOT/reports"
LOG="$ROOT/build/axbench_zybo_run.log"

mkdir -p "$ROOT/build"
find "$REPORTS" -mindepth 1 -delete
mkdir -p "$REPORTS/figures"

cd "$ROOT"
set +e
"$PY" scripts/run_pyals_yosys_suite.py --out-dir "$REPORTS" \
    --jobs "$JOBS" --inner-jobs "$INNER_JOBS" --mem-gb "$MEM_GB" "$@" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e
"$PY" scripts/generate_pyals_scienceplots.py --out-dir "$REPORTS"
echo "Suite exit status: $status (log: $LOG)"
exit "$status"
