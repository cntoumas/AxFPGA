#!/usr/bin/env bash
# Phase 5 ablations of the operator library (historical: run with DSP inference off before that mode was removed): one suite run per AXFPGA_OPS mode.
#   off              no operators (the Phase 3/4 candidate set, with the Phase 5 Tournament fixes)
#   nocomp-nostack   operand/output truncation, single gene, no correction constant
#   nocomp           stacking (all-sites menu and greedy), no correction constant
#   nostack          correction constants, single gene
#   all              the full operator set (also the default run)
# Results: reports/phase5/ablation/<mode>/ and build/phase5/ablation/<mode>/ ; table: scripts/phase5_ablation_table.py
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
# conv2d_3x3 and fft64_psd are left out: one run of each takes about an hour.
DESIGNS="${DESIGNS:-brent_kung_16b kogge_stone_16b fir_filter iir_biquad image_pipeline kinematics_fwd kinematics_fwd_bhv multiplier_16b_bhv multiplier_32b_bhv neural_network neural_network_bhv sobel_filter}"
MODES="${MODES:-off nocomp-nostack nocomp nostack all}"
args=()
for d in $DESIGNS; do args+=(-b "$d"); done
cd "$ROOT"
for mode in $MODES; do
  echo "== ablation mode: $mode"
  AXFPGA_OPS="$mode" "$PY" scripts/run_pyals_yosys_suite.py "${args[@]}" --jobs "${JOBS:-6}" --inner-jobs "${INNER_JOBS:-2}" \
      --mem-gb "${MEM_GB:-11}" --out-dir "reports/phase5/ablation/$mode" --build-dir "build/phase5/ablation/$mode" \
      > "build/phase5_ablation_$mode.log" 2>&1 || echo "mode $mode finished with failures (see build/phase5_ablation_$mode.log)"
done
