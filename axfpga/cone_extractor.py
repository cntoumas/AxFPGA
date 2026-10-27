"""RTLIL Relaxability Inference and Submodule Extractor using Pyosys.

Traverses IEEE 1364/1800 annotated Verilog designs in Pyosys RTLIL,
computes safe combinational cones (C_safe = C_relax \\ C_precise),
and partitions the design into an isolated combinational submodule and top wrapper
using the Yosys `submod` pass.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pyosys import libyosys as ys

logger = logging.getLogger("cone_extractor")


def decode_attr(attr_val: Any, default: Any = None) -> Any:
    """Decode a Yosys RTLIL Const attribute into a Python primitive."""
    if attr_val is None:
        return default
    try:
        s = attr_val.decode_string()
        if s and all(c.isprintable() for c in s):
            return s
    except Exception:
        pass
    if hasattr(attr_val, "as_int"):
        try:
            return attr_val.as_int()
        except Exception:
            pass
    try:
        return attr_val.as_string()
    except Exception:
        return default


class DSU:
    """Disjoint Set Union for wire bit alias resolution."""

    def __init__(self) -> None:
        self.parent: Dict[Tuple[str, int], Tuple[str, int]] = {}

    def find(self, item: Tuple[str, int]) -> Tuple[str, int]:
        if item not in self.parent:
            self.parent[item] = item
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a: Tuple[str, int], b: Tuple[str, int]) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[ra] = rb


class ConeExtractor:
    """Extracts safe approximate combinational cones from annotated Verilog."""

    def __init__(
        self,
        verilog_path: str | Path,
        top_module: Optional[str] = None,
        decompose_operators: bool = False,
    ):
        self.verilog_path = Path(verilog_path).resolve()
        self.top_module_name = top_module
        self.decompose_operators = decompose_operators
        self.design = ys.Design()

    def elaborate(self) -> ys.Module:
        """Elaborate behavioral blocks into RTLIL netlist."""
        top_cmd = f"hierarchy -check -top {self.top_module_name}" if self.top_module_name else "hierarchy -check -auto-top"
        decomp_cmd = "; alumacc; techmap; opt_clean" if self.decompose_operators else ""
        cmd = f"read_verilog -sv {self.verilog_path}; {top_cmd}; proc; opt_expr{decomp_cmd}; opt_clean"
        logger.info("Executing Pyosys elaboration: %s", cmd)
        ys.run_pass(cmd, self.design)

        top = self.design.top_module()
        if top is None:
            raise RuntimeError(f"Could not find top module in {self.verilog_path}")

        self.top_module_name = top.name.str().lstrip("\\")
        return top

    @staticmethod
    def is_sequential_cell(cell: ys.Cell) -> bool:
        """Check if cell is a sequential element (FF, Latch, or Memory)."""
        if cell.is_builtin_ff() or cell.is_mem_cell():
            return True
        cell_type = cell.type.str()
        seq_prefixes = (
            "$dff", "$adff", "$sdff", "$aldff", "$sdffe", "$sdffce",
            "$dlatch", "$sr", "$ff", "$mem", "$_DFF", "$_DLATCH"
        )
        return cell_type.startswith(seq_prefixes)

    def extract_safe_cone(
        self,
        submod_tag: str = "axc_cone_0",
        out_submod_path: Optional[str | Path] = None,
        out_top_path: Optional[str | Path] = None,
        out_meta_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        """Perform relaxability analysis and partition the safe cone."""
        top = self.elaborate()
        modules_before = set(m.str() for m in self.design.modules_.keys())

        dsu = DSU()

        # 1. Resolve wire-to-wire aliases from top.connections()
        for conn in top.connections():
            lhs, rhs = conn[0], conn[1]
            for b_l, b_r in zip(lhs, rhs):
                if b_l.wire and b_r.wire:
                    dsu.union(
                        (b_l.wire.name.str(), b_l.offset),
                        (b_r.wire.name.str(), b_r.offset)
                    )

        # 2. Build cell adjacency maps
        bit_drivers: Dict[Tuple[str, int], str] = {}
        cell_inputs: Dict[str, Set[Tuple[str, int]]] = {}
        cell_outputs: Dict[str, Set[Tuple[str, int]]] = {}
        seq_cells: Set[str] = set()

        for cname_id, cell in top.cells_.items():
            cname = cname_id.str()
            is_seq = self.is_sequential_cell(cell)
            if is_seq:
                seq_cells.add(cname)
            cell_inputs[cname] = set()
            cell_outputs[cname] = set()

            for pname, sig in cell.connections().items():
                is_out = cell.output(pname)
                is_in = cell.input(pname)
                for b in sig:
                    if b.wire:
                        c_b = dsu.find((b.wire.name.str(), b.offset))
                        if is_out:
                            cell_outputs[cname].add(c_b)
                            if not is_seq:
                                bit_drivers[c_b] = cname
                        if is_in:
                            cell_inputs[cname].add(c_b)

        # 3. Collect relaxed and precise seed signals
        relax_seeds: Set[Tuple[str, int]] = set()
        precise_seeds: Set[Tuple[str, int]] = set()
        relaxed_signals_meta: List[Dict[str, Any]] = []

        attr_relax_id = ys.IdString(r"\axc_relax")
        attr_precise_id = ys.IdString(r"\axc_precise")
        attr_metric_id = ys.IdString(r"\axc_metric")
        attr_bound_id = ys.IdString(r"\axc_bound")
        attr_lsb_id = ys.IdString(r"\axc_lsb_width")

        for wname_id, wire in top.wires_.items():
            wname = wname_id.str()
            has_relax = wire.has_attribute(attr_relax_id)
            has_precise = wire.has_attribute(attr_precise_id)

            if has_relax:
                metric = "MED"
                if wire.has_attribute(attr_metric_id):
                    metric = str(decode_attr(wire.attributes[attr_metric_id], "MED"))

                bound = 0.05
                if wire.has_attribute(attr_bound_id):
                    bound_val = decode_attr(wire.attributes[attr_bound_id], 0.05)
                    try:
                        bound = float(bound_val)
                    except (ValueError, TypeError):
                        bound = 0.05

                lsb_w = wire.width
                if wire.has_attribute(attr_lsb_id):
                    lsb_w_val = decode_attr(wire.attributes[attr_lsb_id], wire.width)
                    try:
                        lsb_w = int(lsb_w_val)
                    except (ValueError, TypeError):
                        lsb_w = wire.width
                lsb_w = max(0, min(lsb_w, wire.width))

                relaxed_signals_meta.append({
                    "wire": wname.lstrip("\\"),
                    "width": wire.width,
                    "metric": metric,
                    "bound": bound,
                    "lsb_width": lsb_w,
                })

                if self.decompose_operators and lsb_w < wire.width:
                    for i in range(lsb_w):
                        relax_seeds.add(dsu.find((wname, i)))
                    for i in range(lsb_w, wire.width):
                        precise_seeds.add(dsu.find((wname, i)))
                else:
                    if lsb_w < wire.width:
                        logger.warning(
                            "[AxSYS:WARN] Signal '%s' specifies axc_lsb_width=%d < %d on word-level operators. Slicing requires decompose_operators=True (or --decompose); treating full word as candidate cone.",
                            wname.lstrip("\\"), lsb_w, wire.width
                        )
                    for i in range(wire.width):
                        relax_seeds.add(dsu.find((wname, i)))

            if has_precise:
                for i in range(wire.width):
                    precise_seeds.add(dsu.find((wname, i)))
            elif wire.port_output and not has_relax:
                # Unannotated primary outputs default to precise
                for i in range(wire.width):
                    precise_seeds.add(dsu.find((wname, i)))

        # 4. Handle sequential cells:
        # - Control pins (CLK, ARST, SRST, EN, etc.) are always protected (added to precise_seeds).
        # - If a FF's output Q has relax_seeds, propagate those relax_seeds to the corresponding bits of D,
        #   and do NOT add D to precise_seeds.
        # - If a FF's output Q is unrelaxed / precise, add its D bits to precise_seeds.
        for cname in seq_cells:
            cell = top.cell(ys.IdString(cname))
            relaxed_q_indices: Set[int] = set()
            for pname, sig in cell.connections().items():
                if cell.output(pname):
                    for idx, b in enumerate(sig):
                        if b.wire and dsu.find((b.wire.name.str(), b.offset)) in relax_seeds:
                            relaxed_q_indices.add(idx)

            for pname, sig in cell.connections().items():
                if cell.input(pname):
                    pname_str = pname.str().upper()
                    is_data_in = pname_str in (r"\D", r"\DATA")
                    for idx, b in enumerate(sig):
                        if b.wire:
                            canon_b = dsu.find((b.wire.name.str(), b.offset))
                            if is_data_in and idx in relaxed_q_indices:
                                relax_seeds.add(canon_b)
                            else:
                                precise_seeds.add(canon_b)

        if not relax_seeds:
            logger.warning("No (* axc_relax *) signals found in design %s.", self.top_module_name)
            return {
                "top_module": self.top_module_name,
                "status": "NO_RELAX_SIGNALS",
                "safe_cells": [],
                "relaxed_signals": [],
            }

        # 5. Backward BFS for C_precise
        c_precise: Set[str] = set()
        queue = deque(precise_seeds)
        visited = set(queue)
        while queue:
            b = queue.popleft()
            if b in bit_drivers:
                cell_name = bit_drivers[b]
                if cell_name not in c_precise:
                    c_precise.add(cell_name)
                    for in_b in cell_inputs[cell_name]:
                        if in_b not in visited:
                            visited.add(in_b)
                            queue.append(in_b)

        # 6. Backward BFS for C_relax
        c_relax: Set[str] = set()
        queue = deque(relax_seeds)
        visited = set(queue)
        while queue:
            b = queue.popleft()
            if b in bit_drivers:
                cell_name = bit_drivers[b]
                if cell_name not in c_relax:
                    c_relax.add(cell_name)
                    for in_b in cell_inputs[cell_name]:
                        if in_b not in visited:
                            visited.add(in_b)
                            queue.append(in_b)

        # 7. Safe candidate set
        c_safe = c_relax - c_precise
        logger.info(
            "Cone analysis complete: |C_relax|=%d, |C_precise|=%d, |C_safe|=%d",
            len(c_relax), len(c_precise), len(c_safe)
        )

        if not c_safe:
            logger.warning("Safe cell set is empty. Candidate logic was entirely tainted by precise signals.")
            return {
                "top_module": self.top_module_name,
                "status": "TAINTED_EMPTY_SAFE_SET",
                "safe_cells": [],
                "relaxed_signals": relaxed_signals_meta,
            }

        # Count cell types
        cell_types = Counter()
        for cname in c_safe:
            cell = top.cell(ys.IdString(cname))
            if cell:
                cell_types[cell.type.str()] += 1

        # 8. Partition c_safe into disjoint connected components (Gap 2)
        components = self._find_connected_components(
            c_safe=c_safe,
            cell_inputs=cell_inputs,
            cell_outputs=cell_outputs,
            dsu=dsu,
            relaxed_signals_meta=relaxed_signals_meta,
        )
        logger.info(
            "Partitioned %d safe cells into %d disjoint connected component(s)",
            len(c_safe), len(components)
        )

        # 9. Tag cells with \submod attribute per component
        submod_attr_id = ys.IdString(r"\submod")
        component_tags: List[str] = []
        for idx, comp in enumerate(components):
            tag = submod_tag if len(components) == 1 else f"{submod_tag}_{idx}"
            component_tags.append(tag)
            for cname in comp:
                cell = top.cell(ys.IdString(cname))
                if cell:
                    cell.set_string_attribute(submod_attr_id, tag)

        # 10. Check for sequential feedback loops (Gap 4)
        has_any_feedback = False
        all_feedback_regs: List[str] = []
        cone_feedback_info: Dict[str, Dict[str, Any]] = {}

        for tag, comp in zip(component_tags, components):
            comp_inputs: Set[Tuple[str, int]] = set().union(*(cell_inputs[c] for c in comp))
            comp_outputs: Set[Tuple[str, int]] = set().union(*(cell_outputs[c] for c in comp))
            comp_feedback_regs: List[str] = []

            for cname in seq_cells:
                cell = top.cell(ys.IdString(cname))
                if not cell:
                    continue
                q_bits: Set[Tuple[str, int]] = set()
                d_bits: Set[Tuple[str, int]] = set()
                for pname, sig in cell.connections().items():
                    if cell.output(pname):
                        for b in sig:
                            if b.wire:
                                q_bits.add(dsu.find((b.wire.name.str(), b.offset)))
                    if cell.input(pname):
                        pname_str = pname.str().upper()
                        if pname_str in (r"\D", r"\DATA"):
                            for b in sig:
                                if b.wire:
                                    d_bits.add(dsu.find((b.wire.name.str(), b.offset)))

                if (q_bits & comp_inputs) and (d_bits & comp_outputs):
                    clean_reg = cname.lstrip("\\")
                    q_wire_names = []
                    for pname, sig in cell.connections().items():
                        if cell.output(pname):
                            for b in sig:
                                if b.wire:
                                    q_wire_names.append(b.wire.name.str().lstrip("\\"))
                    reg_desc = f"{clean_reg} ({', '.join(sorted(list(set(q_wire_names))))})" if q_wire_names else clean_reg
                    comp_feedback_regs.append(reg_desc)
                    logger.warning(
                        "[AxSYS:WARN] Sequential feedback loop detected on register '%s' with cone '%s'. DC bias error drift can accumulate over time.",
                        reg_desc, tag
                    )

            has_fb = len(comp_feedback_regs) > 0
            if has_fb:
                has_any_feedback = True
                all_feedback_regs.extend(comp_feedback_regs)

            cone_feedback_info[tag] = {
                "has_sequential_feedback": has_fb,
                "feedback_registers": comp_feedback_regs,
            }

        # 11. Partition via submod pass
        ys.run_pass("submod; opt_clean", self.design)

        modules_after = set(m.str() for m in self.design.modules_.keys())
        new_modules = modules_after - modules_before
        if not new_modules:
            raise RuntimeError("Yosys submod pass failed to create a new submodule module.")

        created_submod_modules = sorted(list(new_modules))
        first_submod_module = created_submod_modules[0]
        first_submod_name = first_submod_module.lstrip("\\")

        # Find instance in top module
        top = self.design.top_module()
        created_instances: List[str] = []
        for cell in top.cells_.values():
            if cell.type.str() in new_modules:
                created_instances.append(cell.name.str().lstrip("\\"))
        first_instance_name = created_instances[0] if created_instances else submod_tag

        # Inspect ports of all created submodules
        cones_meta: List[Dict[str, Any]] = []
        for tag, comp in zip(component_tags, components):
            matched_module_name = None
            for m_str in created_submod_modules:
                clean_m = m_str.lstrip("\\")
                if tag in clean_m:
                    matched_module_name = clean_m
                    break
            if matched_module_name is None:
                matched_module_name = first_submod_name if len(created_submod_modules) == 1 else tag

            submod = self.design.module(ys.IdString("\\" + matched_module_name))
            in_ports = []
            out_ports = []
            if submod:
                for wname_id, wire in submod.wires_.items():
                    if wire.port_input:
                        in_ports.append({"name": wname_id.str().lstrip("\\"), "width": wire.width})
                    if wire.port_output:
                        out_ports.append({"name": wname_id.str().lstrip("\\"), "width": wire.width})

            comp_cell_types = Counter()
            for cname in comp:
                c = top.cell(ys.IdString(cname))
                if c:
                    comp_cell_types[c.type.str()] += 1

            fb = cone_feedback_info.get(tag, {"has_sequential_feedback": False, "feedback_registers": []})
            cones_meta.append({
                "tag": tag,
                "submodule_module": matched_module_name,
                "num_cells": len(comp),
                "cells": sorted(list(comp)),
                "cell_types": dict(comp_cell_types),
                "submodule_inputs": in_ports,
                "submodule_outputs": out_ports,
                "has_sequential_feedback": fb["has_sequential_feedback"],
                "feedback_registers": fb["feedback_registers"],
            })

        # 12. Write output files if requested
        result_files: Dict[str, str] = {}

        if out_submod_path:
            out_submod_p = Path(out_submod_path).resolve()
            out_submod_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_submod_p, "w", encoding="utf-8") as out_f:
                for m in created_submod_modules:
                    clean_m = m.lstrip("\\")
                    tmp_v = out_submod_p.parent / f".tmp_{clean_m}.v"
                    ys.run_pass(
                        f"select -module {clean_m}; write_verilog -selected {tmp_v}; select -clear",
                        self.design
                    )
                    if tmp_v.exists():
                        out_f.write(tmp_v.read_text(encoding="utf-8") + "\n")
                        tmp_v.unlink()
            result_files["submodule_verilog"] = str(out_submod_p)

        if out_top_path:
            out_top_p = Path(out_top_path).resolve()
            out_top_p.parent.mkdir(parents=True, exist_ok=True)
            del_cmd = " ".join([m.lstrip("\\") for m in created_submod_modules])
            ys.run_pass(
                f"delete {del_cmd}; write_verilog {out_top_p}",
                self.design
            )
            result_files["top_shell_verilog"] = str(out_top_p)

        meta_result: Dict[str, Any] = {
            "top_module": self.top_module_name,
            "status": "SUCCESS",
            "submodule_module": first_submod_name,
            "submodule_instance": first_instance_name,
            "num_safe_cells": len(c_safe),
            "safe_cells": sorted(list(c_safe)),
            "cell_types": dict(cell_types),
            "relaxed_signals": relaxed_signals_meta,
            "submodule_inputs": cones_meta[0]["submodule_inputs"] if cones_meta else [],
            "submodule_outputs": cones_meta[0]["submodule_outputs"] if cones_meta else [],
            "cones": cones_meta,
            "has_sequential_feedback": has_any_feedback,
            "feedback_registers": sorted(list(set(all_feedback_regs))),
            "files": result_files,
        }

        if out_meta_path:
            out_meta_p = Path(out_meta_path).resolve()
            out_meta_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_meta_p, "w", encoding="utf-8") as f:
                json.dump(meta_result, f, indent=2)
            result_files["metadata_json"] = str(out_meta_p)

        return meta_result

    def _find_connected_components(
        self,
        c_safe: Set[str],
        cell_inputs: Dict[str, Set[Tuple[str, int]]],
        cell_outputs: Dict[str, Set[Tuple[str, int]]],
        dsu: DSU,
        relaxed_signals_meta: List[Dict[str, Any]],
    ) -> List[Set[str]]:
        """Partition safe cells into disjoint connected components (Gap 2)."""
        if not c_safe:
            return []

        cell_dsu: Dict[str, str] = {c: c for c in c_safe}

        def find_c(x: str) -> str:
            if cell_dsu[x] != x:
                cell_dsu[x] = find_c(cell_dsu[x])
            return cell_dsu[x]

        def union_c(x: str, y: str) -> None:
            rx, ry = find_c(x), find_c(y)
            if rx != ry:
                cell_dsu[rx] = ry

        bit_producers: Dict[Tuple[str, int], List[str]] = {}
        for c in c_safe:
            for b in cell_outputs[c]:
                bit_producers.setdefault(b, []).append(c)

        for c in c_safe:
            for b in cell_inputs[c]:
                if b in bit_producers:
                    for prod_c in bit_producers[b]:
                        if prod_c != c:
                            union_c(c, prod_c)

        for meta in relaxed_signals_meta:
            wname = "\\" + meta["wire"]
            wbits = {dsu.find((wname, i)) for i in range(meta["width"])}
            drivers = [c for c in c_safe if (cell_outputs[c] & wbits)]
            if len(drivers) > 1:
                base = drivers[0]
                for other in drivers[1:]:
                    union_c(base, other)

        comps: Dict[str, Set[str]] = {}
        for c in c_safe:
            root = find_c(c)
            comps.setdefault(root, set()).add(c)

        return sorted(comps.values(), key=lambda s: (-len(s), sorted(list(s))[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Pyosys RTLIL Relaxability Cone Extractor")
    parser.add_argument("-i", "--input", required=True, help="Input annotated Verilog file")
    parser.add_argument("-t", "--top", default=None, help="Top module name (optional)")
    parser.add_argument("--submod-tag", default="axc_cone_0", help="Submodule tag prefix (default: axc_cone_0)")
    parser.add_argument("--out-submod", default=None, help="Path for isolated submodule Verilog")
    parser.add_argument("--out-top", default=None, help="Path for top module shell Verilog")
    parser.add_argument("--out-meta", default=None, help="Path for metadata JSON output")
    parser.add_argument("--decompose", action="store_true", help="Decompose coarse-grain arithmetic cells (alumacc + techmap + splitnets)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    extractor = ConeExtractor(verilog_path=args.input, top_module=args.top, decompose_operators=args.decompose)
    result = extractor.extract_safe_cone(
        submod_tag=args.submod_tag,
        out_submod_path=args.out_submod,
        out_top_path=args.out_top,
        out_meta_path=args.out_meta,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
