#!/usr/bin/env bash
# AxBench evaluation on Zybo Z7-10 (xc7z010clg400-1), Yosys only.
# 1. Empties reports/ (all previous reports and figures are removed).
# 2. Runs all 13 benchmarks: Yosys synthesis, pyALS Tournament, miter + formal SAT,
#    application metrics, activity-based power.
# 3. Renders figures into reports/figures/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-/home/christos-ntoumas/AxSYS/.venv/bin/python}"
REPORTS="$ROOT/reports"
LOG="$ROOT/build/axbench_zybo_run.log"

mkdir -p "$ROOT/build"
find "$REPORTS" -mindepth 1 -delete
mkdir -p "$REPORTS/figures"

cd "$ROOT"
set +e
"$PY" scripts/run_pyals_yosys_suite.py --out-dir "$REPORTS" "$@" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e
"$PY" scripts/generate_pyals_scienceplots.py --out-dir "$REPORTS"
echo "Suite exit status: $status (log: $LOG)"
exit "$status"
