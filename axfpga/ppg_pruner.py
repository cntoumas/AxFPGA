"""Partial Product Generation (PPG) Matrix Anti-Diagonal Pruner for Multipliers.

Part of AxFPGA Plan 2 (Pillar 2).
Targets Wallace-tree multipliers (e.g. Multiplier_15_0_1000, Multiplier_31_0_3000)
composed of UB1BPPG_i_j cells or explicit AND array matrices.

Key Capabilities:
1. Anti-Diagonal Depth Calculation:
   Computes maximum safe truncation depth k such that:
   sum_{w=0}^{k-1} (w + 1) * 2^w <= bound
2. Structural PPG Pruning:
   Replaces UB1BPPG_i_j modules where i + j < k with constant zero assignments:
     assign O = 1'b0;
3. Yosys Downstream Wallace Tree Optimization:
   Invokes Yosys `proc; opt_clean -purge; opt_expr; opt_merge; clean` to eliminate dead downstream
   half-adders and full-adders.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
from axfpga.config import yosys_cmd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("axfpga.ppg_pruner")


class PpgPruner:
    """Prunes partial product generation (PPG) matrices in structural multipliers."""

    def __init__(self, work_dir: Optional[Path] = None):
        self.work_dir = Path(work_dir) if work_dir else Path("/tmp/axfpga_ppg")
        self.work_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def calculate_worst_case_error(depth: int) -> int:
        """Calculate the theoretical maximum worst-case error for anti-diagonal depth k.
        
        WCE = sum_{w=0}^{k-1} (w + 1) * 2^w
        """
        wce = 0
        for w in range(depth):
            terms = w + 1
            wce += terms * (1 << w)
        return wce

    @staticmethod
    def compute_max_safe_depth(bound: float, max_depth: int = 16) -> int:
        """Determine maximum anti-diagonal depth k such that WCE(k) <= bound."""
        safe_k = 0
        for k in range(1, max_depth + 1):
            wce = PpgPruner.calculate_worst_case_error(k)
            if wce <= bound:
                safe_k = k
            else:
                break
        return safe_k

    @staticmethod
    def detect_ppg_modules(code: str) -> List[Tuple[int, int]]:
        """Detect all UB1BPPG_i_j modules present in Verilog source code."""
        matches = re.findall(r"module\s+UB1BPPG_(\d+)_(\d+)\b", code)
        return [(int(m[0]), int(m[1])) for m in matches]

    def prune_code_ppg(self, code: str, depth: int) -> Tuple[str, int]:
        """Prune UB1BPPG_i_j modules where i + j < depth by setting O = 1'b0."""
        pruned_count = 0

        def repl(m: re.Match) -> str:
            nonlocal pruned_count
            i = int(m.group(1))
            j = int(m.group(2))
            if i + j < depth:
                pruned_count += 1
                return (
                    f"module UB1BPPG_{i}_{j}(O, IN1, IN2);\n"
                    f"  output O;\n"
                    f"  input IN1;\n"
                    f"  input IN2;\n"
                    f"  assign O = 1'b0;\n"
                    f"endmodule"
                )
            return m.group(0)

        pattern = r"module\s+UB1BPPG_(\d+)_(\d+)\s*\([^)]*\);.*?endmodule"
        modified_code = re.sub(pattern, repl, code, flags=re.DOTALL)
        return modified_code, pruned_count

    def generate_candidates(
        self,
        cone_content: str,
        bound: float,
        out_dir: Optional[Path] = None,
        top_module: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Generate candidate netlists with varying PPG truncation depths."""
        target_dir = out_dir or self.work_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        candidates: List[Dict[str, Any]] = []

        ppg_list = self.detect_ppg_modules(cone_content)
        if not ppg_list:
            logger.debug("No UB1BPPG modules detected in cone content; skipping PPG pruning.")
            return candidates

        logger.info("Detected %d UB1BPPG modules in cone. Evaluating PPG truncation depths...", len(ppg_list))
        safe_max_k = self.compute_max_safe_depth(bound, max_depth=16)
        if safe_max_k < 1:
            safe_max_k = 1

        # Generate candidates for depths from 2 up to safe_max_k (or at least safe_max_k)
        depths_to_test = sorted(set([k for k in [2, 3, 4, 5, 6, safe_max_k] if k <= safe_max_k and k > 0]))
        if safe_max_k not in depths_to_test:
            depths_to_test.append(safe_max_k)

        for k in depths_to_test:
            mod_code, num_pruned = self.prune_code_ppg(cone_content, depth=k)
            if num_pruned == 0:
                continue

            raw_cand_path = target_dir / f"cand_ppg_raw_k{k}.v"
            raw_cand_path.write_text(mod_code, encoding="utf-8")

            # Run Yosys dead logic elimination to collapse Wallace tree
            opt_cand_path = target_dir / f"cand_ppg_k{k}.v"
            top_opt = f"-top {top_module}" if top_module else "-auto-top"
            ys_cmd = (
                f"read_verilog -sv {raw_cand_path}; "
                f"hierarchy -check {top_opt}; "
                f"proc; opt_clean -purge; opt_expr; opt_merge; clean; "
                f"write_verilog -noattr {opt_cand_path}"
            )
            res = subprocess.run(yosys_cmd() + ["-q", "-p", ys_cmd], capture_output=True, text=True)

            final_cand_path = opt_cand_path if (res.returncode == 0 and opt_cand_path.exists()) else raw_cand_path
            theoretical_wce = self.calculate_worst_case_error(k)

            cand = {
                "name": f"pyALS_ppg_k{k}",
                "path": final_cand_path,
                "strategy": "pyals",
                "method": "ppg_pruner",
                "depth": k,
                "pruned_ppgs": num_pruned,
                "theoretical_wce": theoretical_wce,
            }
            candidates.append(cand)
            logger.info(
                "Emitted PPG candidate 'pyALS_ppg_k%d': pruned %d PPGs (theoretical WCE=%d <= bound %.1f)",
                k, num_pruned, theoretical_wce, bound
            )

        return candidates
