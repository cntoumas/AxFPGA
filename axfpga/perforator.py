"""RTL Loop & Tap Perforation Engine for AxSYS.

Implements structural approximate computing across three hardware paradigms:
1. Spatial Loop / Tap Perforation:
   Drops low-weight arithmetic terms in multi-operand adder trees and unrolled stencils
   (e.g., FIR filters `p0 + p1 + p2 + p3`, image stencils, systolic arrays).
2. Sequential Loop Perforation (Early Iteration Termination):
   Truncates multi-cycle FSM loop counters (`count == N` -> `count == N - k`),
   reducing execution cycles and latency in iterative algorithms.
3. Temporal Stride Decimation:
   Downsamples streaming pipelines (`valid_in` sampling every K cycles)
   for dynamic switching power reduction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("axfpga.perforator")


@dataclass
class SpatialSumTree:
    """Represents an extracted multi-operand summation tree."""
    line: str
    target_signal: str
    terms: List[str]
    is_signed: bool
    bit_width: int
    raw_statement: str


@dataclass
class SequentialCounterLoop:
    """Represents an extracted sequential iteration counter loop."""
    counter_name: str
    bound_value: Any
    comparator_op: str
    bit_width: int
    raw_condition: str
    is_numeric: bool = True


class RtlLoopPerforator:
    """Hardware RTL Loop, Tap, and Stride Perforation Engine."""

    def __init__(self, verilog_path: str | Path, work_dir: Optional[str | Path] = None):
        self.verilog_path = Path(verilog_path).resolve()
        self.work_dir = Path(work_dir).resolve() if work_dir else self.verilog_path.parent
        self.raw_content = self.verilog_path.read_text(encoding="utf-8")

    # -------------------------------------------------------------------------
    # 1. Spatial Tap / Adder Tree Discovery & Perforation
    # -------------------------------------------------------------------------
    def detect_spatial_sum_trees(self) -> List[SpatialSumTree]:
        """Discover multi-operand adder trees (>= 3 terms) in RTL."""
        trees: List[SpatialSumTree] = []

        # Match assign or wire declaration:
        # e.g.: assign sum = p0 + p1 + p2 + p3;
        # or: wire signed [17:0] sum = p0 + p1 + p2 + p3;
        assign_patterns = [
            r"assign\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);",
            r"wire\s+(signed\s+)?(\[[^\]]+\]\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);",
        ]

        for pat in assign_patterns:
            for match in re.finditer(pat, self.raw_content):
                if pat.startswith("assign"):
                    target = match.group(1)
                    expr = match.group(2).strip()
                    is_signed = False
                    width = 32
                else:
                    is_signed = bool(match.group(1))
                    width_str = match.group(2)
                    target = match.group(3)
                    expr = match.group(4).strip()
                    width = 32
                    if width_str:
                        w_m = re.match(r"\[(\d+):(\d+)\]", width_str.strip())
                        if w_m:
                            width = abs(int(w_m.group(1)) - int(w_m.group(2))) + 1

                # Check if expression contains multiple additions: term1 + term2 + term3 ...
                # Simple balanced parenthesis splitter on '+'
                terms = self._split_addition_terms(expr)
                if len(terms) >= 3:
                    trees.append(
                        SpatialSumTree(
                            line=match.group(0),
                            target_signal=target,
                            terms=terms,
                            is_signed=is_signed,
                            bit_width=width,
                            raw_statement=match.group(0),
                        )
                    )

        return trees

    def _split_addition_terms(self, expr: str) -> List[str]:
        """Split expression into top-level additive terms."""
        terms = []
        depth = 0
        current = []
        for char in expr:
            if char in "([{":
                depth += 1
                current.append(char)
            elif char in ")]}":
                depth -= 1
                current.append(char)
            elif char == "+" and depth == 0:
                t = "".join(current).strip()
                if t:
                    terms.append(t)
                current = []
            else:
                current.append(char)
        t = "".join(current).strip()
        if t:
            terms.append(t)
        return terms

    def perforate_spatial_tree(
        self,
        tree: SpatialSumTree,
        num_taps_to_drop: int,
        out_path: Optional[Path] = None,
        drop_from: str = "end",  # 'end', 'start', 'smallest'
    ) -> Path:
        """Drop `num_taps_to_drop` taps from a spatial summation tree."""
        if num_taps_to_drop >= len(tree.terms):
            raise ValueError(f"Cannot drop {num_taps_to_drop} taps from {len(tree.terms)} total terms")

        out_path = out_path or (self.work_dir / f"perforated_spatial_{tree.target_signal}_drop{num_taps_to_drop}.v")
        remaining_terms = list(tree.terms)

        if drop_from == "end":
            dropped = remaining_terms[-num_taps_to_drop:]
            remaining_terms = remaining_terms[:-num_taps_to_drop]
        elif drop_from == "start":
            dropped = remaining_terms[:num_taps_to_drop]
            remaining_terms = remaining_terms[num_taps_to_drop:]
        else:
            # Drop from ends (alternate outer taps)
            dropped = []
            for _ in range(num_taps_to_drop):
                if len(dropped) % 2 == 0:
                    dropped.append(remaining_terms.pop(-1))
                else:
                    dropped.append(remaining_terms.pop(0))

        new_expr = " + ".join(remaining_terms)
        orig_stmt = tree.raw_statement
        # Replace right-hand side of statement
        if orig_stmt.startswith("assign"):
            new_stmt = f"assign {tree.target_signal} = {new_expr}; // [Perforated: dropped {', '.join(dropped)}]"
        else:
            signed_str = "signed " if tree.is_signed else ""
            width_str = f"[{tree.bit_width-1}:0] " if tree.bit_width > 1 else ""
            new_stmt = f"wire {signed_str}{width_str}{tree.target_signal} = {new_expr}; // [Perforated: dropped {', '.join(dropped)}]"

        mutated_code = self.raw_content.replace(orig_stmt, new_stmt)
        out_path.write_text(mutated_code, encoding="utf-8")
        logger.info("Generated spatial perforated RTL: dropped %s -> %s", dropped, out_path.name)
        return out_path

    # -------------------------------------------------------------------------
    # 2. Sequential Loop Counter Discovery & Early Termination
    # -------------------------------------------------------------------------
    def _find_if_conditions(self) -> List[Tuple[int, int, str, str]]:
        """Find all if (...) conditions in RTL with balanced parentheses.

        Returns list of (start_idx, end_idx, cond_text, full_if_stmt).
        """
        results = []
        for m in re.finditer(r"\bif\s*\(", self.raw_content):
            start = m.end() - 1  # index of '('
            depth = 0
            end = start
            for i in range(start, len(self.raw_content)):
                if self.raw_content[i] == "(":
                    depth += 1
                elif self.raw_content[i] == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if depth == 0:
                cond = self.raw_content[start + 1 : end].strip()
                results.append((m.start(), end + 1, cond, self.raw_content[m.start() : end + 1]))
        return results

    def detect_sequential_counters(self) -> List[SequentialCounterLoop]:
        """Discover multi-cycle iterative FSM counters and termination conditions.

        Supports both literal numeric bounds (e.g. `count == 12`) and
        parameterized symbolic bounds (e.g. `stage_idx == LOG2_N - 1`, `bfy_count == (N/2) - 1`).
        """
        counters: List[SequentialCounterLoop] = []

        # Find registers incremented or decremented:
        # count <= count + 1'b1; or count <= count + 1; or count <= count - 1;
        step_matches = re.finditer(
            r"([A-Za-z_][A-Za-z0-9_]*)\s*<=\s*\1\s*(\+|\-)\s*([0-9]+'[bBdDhHoO][0-9a-fA-F_]+|[0-9]+);",
            self.raw_content,
        )
        counter_names = set(m.group(1) for m in step_matches)
        if not counter_names:
            return counters

        if_blocks = self._find_if_conditions()
        seen = set()

        for _, _, cond_text, _ in if_blocks:
            for c_name in counter_names:
                # Match comparisons: c_name == expr, c_name >= expr, etc.
                cmp_pat = rf"\b{c_name}\s*(==|!=|>=|>|<=|<)\s*([A-Za-z0-9_\'\+\-\*/\(\)\s]+)"
                for m_cmp in re.finditer(cmp_pat, cond_text):
                    op = m_cmp.group(1)
                    raw_bound = m_cmp.group(2).strip()
                    # Clean up bound: strip trailing logic connectors
                    raw_bound = re.split(r"\s*(?:&&|\|\|)\s*", raw_bound)[0].strip()
                    raw_cond = f"{c_name} {op} {raw_bound}"

                    if (c_name, raw_cond) in seen:
                        continue
                    seen.add((c_name, raw_cond))

                    # Parse bound as numeric if possible
                    is_num = False
                    val: Any = raw_bound
                    if re.fullmatch(r"[0-9]+", raw_bound):
                        val = int(raw_bound)
                        is_num = True
                    elif "'d" in raw_bound:
                        try:
                            val = int(raw_bound.split("'d")[1])
                            is_num = True
                        except ValueError:
                            pass
                    elif "'h" in raw_bound:
                        try:
                            val = int(raw_bound.split("'h")[1], 16)
                            is_num = True
                        except ValueError:
                            pass

                    counters.append(
                        SequentialCounterLoop(
                            counter_name=c_name,
                            bound_value=val,
                            comparator_op=op,
                            bit_width=32,
                            raw_condition=raw_cond,
                            is_numeric=is_num,
                        )
                    )

        return counters

    def perforate_sequential_counter(
        self,
        loop: SequentialCounterLoop,
        cycle_reduction: int = 1,
        new_bound_expr: Optional[str] = None,
        out_path: Optional[Path] = None,
    ) -> Path:
        """Perforate sequential iteration counter by reducing loop bound.

        Supports both numeric integer bounds and parameterized symbolic expressions.
        """
        orig_cond = loop.raw_condition

        if new_bound_expr is not None:
            new_cond = f"{loop.counter_name} {loop.comparator_op} {new_bound_expr}"
            bound_desc = str(new_bound_expr)
        elif loop.is_numeric:
            if cycle_reduction >= loop.bound_value:
                raise ValueError(f"Cycle reduction {cycle_reduction} must be less than bound {loop.bound_value}")
            new_bound = loop.bound_value - cycle_reduction
            bound_desc = str(new_bound)
            # Preserve Verilog radix formatting if present
            if "'d" in orig_cond:
                parts = orig_cond.split("'d")
                width_prefix = parts[0].split()[-1] + "'d"
                new_cond = re.sub(rf"{width_prefix}\d+", f"{width_prefix}{new_bound}", orig_cond)
            else:
                new_cond = re.sub(rf"\b{loop.bound_value}\b", str(new_bound), orig_cond)
        else:
            # Parameterized symbolic bound (e.g. LOG2_N - 1, (N/2) - 1)
            raw_bound = str(loop.bound_value)
            m_sub = re.match(r"^(.*?)\s*-\s*(\d+)$", raw_bound)
            m_add = re.match(r"^(.*?)\s*\+\s*(\d+)$", raw_bound)
            if m_sub:
                base = m_sub.group(1).strip()
                curr_off = int(m_sub.group(2))
                new_bound_val = f"{base} - {curr_off + cycle_reduction}"
            elif m_add:
                base = m_add.group(1).strip()
                curr_off = int(m_add.group(2))
                rem = curr_off - cycle_reduction
                new_bound_val = f"{base} + {rem}" if rem > 0 else (base if rem == 0 else f"{base} - {abs(rem)}")
            else:
                new_bound_val = f"{raw_bound} - {cycle_reduction}"

            bound_desc = new_bound_val
            new_cond = f"{loop.counter_name} {loop.comparator_op} {new_bound_val}"

        out_path = out_path or (self.work_dir / f"perforated_seq_{loop.counter_name}_minus{cycle_reduction}.v")

        mutated_code = self.raw_content.replace(
            orig_cond,
            f"{new_cond} /* [Perforated: early exit -{cycle_reduction} cycles] */",
        )
        if mutated_code == self.raw_content:
            # Fallback to regex with flexible whitespace
            orig_re = (
                re.escape(loop.counter_name)
                + r"\s*"
                + re.escape(loop.comparator_op)
                + r"\s*"
                + re.escape(str(loop.bound_value))
            )
            mutated_code = re.sub(
                orig_re,
                f"{new_cond} /* [Perforated: early exit -{cycle_reduction} cycles] */",
                self.raw_content,
            )

        out_path.write_text(mutated_code, encoding="utf-8")
        logger.info(
            "Generated sequential perforated RTL: condition '%s' -> '%s' (bound=%s) -> %s",
            orig_cond, new_cond, bound_desc, out_path.name
        )
        return out_path

    # -------------------------------------------------------------------------
    # 3. Temporal Stream Decimation (Sample Stride Perforation)
    # -------------------------------------------------------------------------
    def perforate_temporal_decimation(
        self,
        stride: int = 2,
        valid_in_name: str = "valid_in",
        clk_name: str = "clk",
        rst_n_name: str = "rst_n",
        out_path: Optional[Path] = None,
    ) -> Path:
        """Introduce temporal stride decimation on streaming RTL input interface."""
        out_path = out_path or (self.work_dir / f"perforated_temporal_stride{stride}.v")

        # Add decimation register and stride logic
        reg_width = (stride - 1).bit_length()
        decim_code = f"""
  // [Temporal Stride Perforation (1/{stride} Downsampling)]
  reg [{reg_width-1}:0] _axc_stride_counter;
  wire _axc_stride_enable = ({valid_in_name}) && (_axc_stride_counter == {reg_width}'d0);

  always @(posedge {clk_name} or negedge {rst_n_name}) begin
    if (!{rst_n_name}) begin
      _axc_stride_counter <= {reg_width}'d0;
    end else if ({valid_in_name}) begin
      if (_axc_stride_counter == {reg_width}'d{stride - 1})
        _axc_stride_counter <= {reg_width}'d0;
      else
        _axc_stride_counter <= _axc_stride_counter + 1'b1;
    end
  end
"""
        # Inject decimation logic right after module port declaration
        mod_m = re.search(r"module\s+[A-Za-z0-9_]+\s*\([^;]*\);", self.raw_content, re.DOTALL)
        if not mod_m:
            raise ValueError(f"Could not parse module header in {self.verilog_path}")

        hdr_end = mod_m.end()
        mutated_code = self.raw_content[:hdr_end] + decim_code + self.raw_content[hdr_end:]

        # Gate internal logic with _axc_stride_enable instead of raw valid_in
        mutated_code = re.sub(
            rf"\bif\s*\(\s*{valid_in_name}\s*\)",
            "if (_axc_stride_enable)",
            mutated_code,
        )

        out_path.write_text(mutated_code, encoding="utf-8")
        logger.info("Generated temporal decimation RTL: stride %d -> %s", stride, out_path.name)
        return out_path

    # -------------------------------------------------------------------------
    # 4. Automated Candidate Generation
    # -------------------------------------------------------------------------
    def generate_all_candidates(self) -> List[Dict[str, Any]]:
        """Generate all viable spatial, sequential, and temporal perforated mutants."""
        candidates: List[Dict[str, Any]] = []

        # 1. Spatial Trees
        trees = self.detect_spatial_sum_trees()
        for tree in trees:
            max_drops = min(len(tree.terms) - 2, 3)
            for k_drop in range(1, max_drops + 1):
                p_mut = self.perforate_spatial_tree(tree, num_taps_to_drop=k_drop, drop_from="end")
                candidates.append({
                    "name": f"SpatialPerforate_{tree.target_signal}_drop{k_drop}",
                    "path": p_mut,
                    "type": "spatial",
                    "dropped_taps": k_drop,
                    "target_signal": tree.target_signal,
                })

        # 2. Sequential Counters
        counters = self.detect_sequential_counters()
        for counter in counters:
            if counter.is_numeric:
                if counter.bound_value >= 4:
                    for dec in [1, 2, counter.bound_value // 4]:
                        if 0 < dec < counter.bound_value:
                            p_seq = self.perforate_sequential_counter(counter, cycle_reduction=dec)
                            candidates.append({
                                "name": f"SeqPerforate_{counter.counter_name}_minus{dec}",
                                "path": p_seq,
                                "type": "sequential",
                                "cycle_reduction": dec,
                                "counter": counter.counter_name,
                            })
            else:
                # Parameterized symbolic bound
                for dec in [1, 2]:
                    p_seq = self.perforate_sequential_counter(counter, cycle_reduction=dec)
                    candidates.append({
                        "name": f"SeqPerforate_{counter.counter_name}_minus{dec}",
                        "path": p_seq,
                        "type": "sequential",
                        "cycle_reduction": dec,
                        "counter": counter.counter_name,
                    })

        # 3. Temporal Decimation
        if "valid_in" in self.raw_content:
            for stride in [2, 4]:
                try:
                    p_temp = self.perforate_temporal_decimation(stride=stride)
                    candidates.append({
                        "name": f"TemporalDecimate_stride{stride}",
                        "path": p_temp,
                        "type": "temporal",
                        "stride": stride,
                    })
                except Exception as exc:
                    logger.debug("Temporal decimation skipped: %s", exc)

        return candidates
