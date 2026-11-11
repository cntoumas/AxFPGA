"""Candidate Tournament."""

from __future__ import annotations

import logging
import random
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from axfpga import cache
from axfpga.config import yosys_cmd

from axfpga.cone_interface import parse_cone_interface
from axfpga.estimator import FpgaEstimator

logger = logging.getLogger("axfpga.tournament")

Generator = Callable[[Dict[str, Any], Path, float], List[Dict[str, Any]]]

DSP_LC_WEIGHT = 20


@dataclass(frozen=True)
class Entry:
    """One evaluated Candidate."""

    name: str
    error: float
    passed_bound: bool
    lcs: Optional[int] = None
    luts: Optional[int] = None
    dsps: Optional[int] = None


@dataclass(frozen=True)
class Alternative:
    """A Candidate that met the Bound and saved resources on the Cone (unremapped netlist)."""

    name: str
    netlist_path: Path
    error: float
    lcs: int
    luts: int
    carry4: int
    dsps: int = 0


@dataclass(frozen=True)
class Champion:
    """Result of a Tournament run."""

    name: str
    netlist_path: Path
    error: float
    metric: str
    bound: float
    lcs: int
    luts: int
    carry4: int
    baseline_lcs: int
    baseline_luts: int
    baseline_carry4: int
    lc_savings_pct: float
    fell_back: bool
    leaderboard: Tuple[Entry, ...]
    # All qualifying Candidates, best first (alternatives[0] is the Champion unless fell_back)
    alternatives: Tuple[Alternative, ...] = ()


class Tournament:
    """Selects the Champion Candidate for a Cone under an error Bound."""

    def __init__(self, estimator: FpgaEstimator, work_dir: str | Path, sim_samples: int = 256, jobs: int = 1):
        self.estimator = estimator
        self.jobs = max(1, int(jobs))
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.sim_samples = sim_samples

    def run(
        self,
        cone_vlog: str | Path,
        meta_json: Optional[str | Path],
        metric: str,
        bound: float,
        generators: Sequence[Generator],
        trace_hex: Optional[str | Path] = None,
    ) -> Champion:
        cone_vlog = Path(cone_vlog).resolve()
        logger.info("=== Tournament: %s | Bound: %s=%.2f ===", cone_vlog.name, metric, bound)

        iface = parse_cone_interface(cone_vlog, meta_json)
        exact_v = self._write_exact_cone(iface)
        iface["exact_netlist"] = exact_v
        top = iface["module_name"]

        base = self.estimator.evaluate_netlist(exact_v, top_module=top)
        base_lcs = base.get("estimated_lcs", 100)
        base_luts = base.get("total_luts", 0)
        base_c4 = base.get("carry4", 0)
        base_dsps = base.get("dsps", 0)

        candidates: List[Dict[str, Any]] = []
        for gen in generators:
            try:
                candidates.extend(gen(iface, self.work_dir, bound))
            except Exception as exc:
                logger.warning("Generator %r failed: %s", gen, exc)
        logger.info("Evaluating %d candidates", len(candidates))

        stim_hex = self._prepare_stimulus_dataset(iface, self.work_dir, trace_hex)

        leaderboard: List[Entry] = []
        valid: List[Dict[str, Any]] = []

        def evaluate(cand: Dict[str, Any]) -> Tuple[Entry, Optional[Dict[str, Any]]]:
            """Error and cost of one candidate on the cone: its leaderboard entry and, if within the bound, a record."""
            sim_res = self._simulate_candidate(exact_v, Path(cand["path"]), iface, stim_hex, metric, self.work_dir)
            err = sim_res.get(metric.lower(), 1e9)
            if err > bound:
                return Entry(cand["name"], err, False), None
            cost = self.estimator.evaluate_netlist(cand["path"], top_module=top)
            rec = {
                "name": cand["name"],
                "path": Path(cand["path"]),
                "error": err,
                "lcs": cost.get("estimated_lcs", 9999),
                "luts": cost.get("total_luts", 0),
                "carry4": cost.get("carry4", 0),
                "dsps": cost.get("dsps", 0),
            }
            if "genome" in cand:
                rec["genome"] = cand["genome"]
            return Entry(rec["name"], err, True, rec["lcs"], rec["luts"], rec["dsps"]), rec

        def score_many(cands: Sequence[Dict[str, Any]]) -> List[Optional[Dict[str, Any]]]:
            """Evaluate candidates (concurrently with ``jobs`` > 1); results and leaderboard keep the input order."""
            if self.jobs > 1 and len(cands) > 1:
                with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                    results = list(pool.map(evaluate, cands))
            else:
                results = [evaluate(c) for c in cands]
            for entry, _ in results:
                leaderboard.append(entry)
            return [rec for _, rec in results]

        valid.extend(r for r in score_many(candidates) if r is not None)

        # Generators that stack their own operators (greedy refinement of the best candidates) get the scoring function.
        for gen in generators:
            refine = getattr(gen, "refine", None)
            if refine is None:
                continue
            try:
                valid.extend(refine(iface, self.work_dir, bound, list(valid), score_many, base_lcs))
            except Exception as exc:
                logger.warning("Refinement by %r failed: %s", gen, exc)

        def cost_of(m: Dict[str, Any]) -> int:
            return m["lcs"] + DSP_LC_WEIGHT * m["dsps"]

        base_cost = base_lcs + DSP_LC_WEIGHT * base_dsps
        # Fewer LUTs alone is not a saving when the logic-cell count (LUT + 2 x CARRY4) grows: carry chains count.
        beneficial = [m for m in valid if cost_of(m) < base_cost
                      or (m["lcs"] <= base_lcs and m["luts"] < base_luts and m["dsps"] <= base_dsps)]
        if beneficial:
            # The name is the final key: equal-cost candidates must not depend on generation order.
            beneficial.sort(key=lambda m: (cost_of(m), m["luts"], m["error"], m["name"]))
            champ = beneficial[0]
            fell_back = False
            logger.info("CHAMPION: %s | LCs %d -> %d | %s=%.2f", champ["name"], base_lcs, champ["lcs"], metric, champ["error"])
        else:
            logger.warning("No candidate met bound %s=%.2f with savings; falling back to exact cone", metric, bound)
            champ = {"name": "baseline_fallback", "path": exact_v, "error": 0.0,
                     "lcs": base_lcs, "luts": base_luts, "carry4": base_c4, "dsps": base_dsps}
            fell_back = True

        savings = round((base_lcs - champ["lcs"]) / base_lcs * 100.0, 2) if (base_lcs > 0 and not fell_back) else 0.0

        return Champion(
            name=champ["name"],
            netlist_path=self.remap(champ["path"]),
            error=champ["error"],
            metric=metric,
            bound=bound,
            lcs=champ["lcs"],
            luts=champ["luts"],
            carry4=champ["carry4"],
            baseline_lcs=base_lcs,
            baseline_luts=base_luts,
            baseline_carry4=base_c4,
            lc_savings_pct=savings,
            fell_back=fell_back,
            leaderboard=tuple(leaderboard),
            alternatives=tuple(
                Alternative(m["name"], m["path"], m["error"], m["lcs"], m["luts"], m["carry4"], m["dsps"]) for m in beneficial
            ),
        )

    def _write_exact_cone(self, iface: Dict[str, Any]) -> Path:
        """Write the exact Cone, tying floating relaxed-bus bits to their auxiliary ports."""
        exact_v = self.work_dir / "exact_cone_clean.v"
        mod_content = iface["raw_content"]
        rel_out = iface.get("relaxed_output")
        outputs = iface.get("outputs", {})
        if rel_out and rel_out in outputs:
            rel_info = outputs[rel_out]
            rel_w = rel_info["width"]
            rel_lsb = rel_info.get("lsb", 0)
            rel_msb = rel_info.get("msb", rel_w - 1)
            rel_fid = f"{rel_out} " if rel_out.startswith("\\") and not rel_out.endswith(" ") else rel_out
            clean_rel = rel_out.lower().lstrip("\\")
            for out_name, out_info in outputs.items():
                if out_name == rel_out:
                    continue
                clean_n = out_name.lower()
                out_fid = f"{out_name} " if out_name.startswith("\\") and not out_name.endswith(" ") else out_name
                if out_info["width"] == 1 and ("po" in clean_n or "p0" in clean_n):
                    if f"assign {rel_fid}[{rel_lsb}]" not in mod_content and f"assign {rel_out} [{rel_lsb}]" not in mod_content:
                        mod_content = mod_content.replace("endmodule", f"  assign {rel_fid}[{rel_lsb}] = {out_fid};\nendmodule")
                elif out_info["width"] == 1 and ("go" in clean_n or "g0" in clean_n or "cout" in clean_n or "co" in clean_n):
                    if f"assign {rel_fid}[{rel_msb}]" not in mod_content and f"assign {rel_out} [{rel_msb}]" not in mod_content:
                        mod_content = mod_content.replace("endmodule", f"  assign {rel_fid}[{rel_msb}] = {out_fid};\nendmodule")
                elif clean_n.endswith(f".{clean_rel}") or clean_n == clean_rel:
                    for b in range(rel_lsb, rel_msb + 1):
                        if f"assign {rel_fid}[{b}]" not in mod_content and f"assign {rel_out} [{b}]" not in mod_content:
                            mod_content = mod_content.replace("endmodule", f"  assign {rel_fid}[{b}] = {out_fid}[{b}];\nendmodule")
                        if f"assign {out_fid}[{b}]" not in mod_content and f"assign {out_name} [{b}]" not in mod_content:
                            mod_content = mod_content.replace("endmodule", f"  assign {out_fid}[{b}] = {rel_fid}[{b}];\nendmodule")
        exact_v.write_text(mod_content, encoding="utf-8")
        return exact_v

    def remap(self, netlist: Path, out_path: Optional[Path] = None) -> Path:
        """Xilinx 7-Series Yosys re-mapping pass on the Champion netlist."""
        remap_out = Path(out_path) if out_path else self.work_dir / "remapped_approx_cone.v"
        nodsp = bool(getattr(self.estimator, "nodsp", False))
        ckey = cache.make_key("remap", [netlist], family=self.estimator.family, nodsp=nodsp)
        hit = cache.load("remap", ckey)
        if hit is not None:
            if hit[0] == "ok" and cache.restore(hit[1], "remap.v", remap_out):
                return remap_out
            if hit[0] == "unchanged":
                return Path(netlist)
        remap_cmd = (
            f"read_verilog -sv {netlist}; "
            f"hierarchy -check -auto-top; "
            f"opt_clean -purge; "
            f"opt -mux_undef -mux_bool -undriven -fine; "
            f"synth_xilinx -family {self.estimator.family} -noiopad {'-nodsp ' if nodsp else ''}-run begin:check; "
            f"opt_clean -purge; "
            f"write_verilog -noattr {remap_out}"
        )
        res = subprocess.run(yosys_cmd() + ["-q", "-p", remap_cmd], capture_output=True, text=True)
        if res.returncode == 0 and remap_out.exists():
            cache.save("remap", ckey, "ok", {"remap.v": remap_out})
            return remap_out
        if res.returncode != 0:
            cache.save("remap", ckey, "unchanged")
        return Path(netlist)

    def _prepare_stimulus_dataset(self, iface: Dict[str, Any], run_dir: Path, trace_hex: Optional[str | Path] = None) -> Path:
        """Prepare stimulus vectors hex file for dual-miter simulation."""
        stim_hex = run_dir / "sim_stimulus.hex"
        if trace_hex and Path(trace_hex).exists():
            shutil.copy(trace_hex, stim_hex)
            return stim_hex

        # Generate synthetic stimulus vectors
        py_rnd = random.Random(42)
        lines = []
        input_names = list(iface["inputs"].keys())
        lines.append("# " + " ".join(input_names))

        for _ in range(self.sim_samples):
            vec_parts = []
            for name in input_names:
                info = iface["inputs"][name]
                w = info["width"]
                if w == 1:
                    val = py_rnd.choice([0, 1])
                else:
                    max_val = (1 << w) - 1
                    val = py_rnd.randint(0, max_val)
                vec_parts.append(f"{val:x}")
            lines.append(" ".join(vec_parts))

        stim_hex.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return stim_hex

    def _simulate_candidate(
        self,
        exact_vlog: Path,
        approx_vlog: Path,
        iface: Dict[str, Any],
        stim_hex: Path,
        metric: str,
        run_dir: Path,
    ) -> Dict[str, float]:
        """Run cycle-accurate dual miter testbench via iverilog and compute error (cached by content)."""
        ckey = cache.make_key("tsim", [exact_vlog, approx_vlog, stim_hex], iface=iface, metric=metric, tb="p5-decoded-diff")
        hit = cache.load("tsim", ckey)
        if hit is not None:
            return dict(hit[0])
        res = self._simulate_candidate_uncached(exact_vlog, approx_vlog, iface, stim_hex, metric, run_dir)
        if res.get("med") != 1e9:                 # compile / run failures are not cached
            cache.save("tsim", ckey, dict(res))
        return res

    def _simulate_candidate_uncached(
        self,
        exact_vlog: Path,
        approx_vlog: Path,
        iface: Dict[str, Any],
        stim_hex: Path,
        metric: str,
        run_dir: Path,
    ) -> Dict[str, float]:
        """Run cycle-accurate dual miter testbench via iverilog and compute error."""
        tb_v = run_dir / f"tb_eval_{approx_vlog.stem}.v"
        vvp_bin = run_dir / f"tb_eval_{approx_vlog.stem}.vvp"

        # Rename candidate module to avoid collision with exact cone module
        approx_mod_name = f"{iface['module_name']}_approx"
        approx_renamed_v = run_dir / f"{approx_vlog.stem}_renamed.v"
        cand_content = approx_vlog.read_text(encoding="utf-8")
        renamed_content = re.sub(rf"\bmodule\s+{iface['module_name']}\b", f"module {approx_mod_name}", cand_content, count=1)
        approx_renamed_v.write_text(renamed_content, encoding="utf-8")

        inputs = iface["inputs"]
        outputs = iface["outputs"]
        rel_out = iface["relaxed_output"]
        rel_width = outputs[rel_out]["width"]
        is_sub = "sub" in iface["module_name"].lower() or "diff" in rel_out.lower() or "sub" in rel_out.lower()
        rel_signed = outputs[rel_out].get("signed", False) or is_sub

        # Build clean testbench
        inp_decls = []
        inst_exact_conns = []
        inst_approx_conns = []
        read_conns = []

        for name, info in inputs.items():
            w = info["width"]
            msb = info.get("msb", w - 1)
            lsb = info.get("lsb", 0)
            clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name.lstrip("\\"))
            inp_decls.append(f"  reg [{msb}:{lsb}] in_{clean_name};" if w > 1 else f"  reg in_{clean_name};")
            inst_exact_conns.append(f".{name} (in_{clean_name})")
            inst_approx_conns.append(f".{name} (in_{clean_name})")
            read_conns.append(f"in_{clean_name}")

        out_decls = []
        for name, info in outputs.items():
            w = info["width"]
            msb = info.get("msb", w - 1)
            lsb = info.get("lsb", 0)
            clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", name.lstrip("\\"))
            out_decls.append(f"  wire [{msb}:{lsb}] exact_{clean_name};" if w > 1 else f"  wire exact_{clean_name};")
            out_decls.append(f"  wire [{msb}:{lsb}] approx_{clean_name};" if w > 1 else f"  wire approx_{clean_name};")
            inst_exact_conns.append(f".{name} (exact_{clean_name})")
            inst_approx_conns.append(f".{name} (approx_{clean_name})")

        format_str = " ".join(["%x"] * len(inputs))
        read_args = ", ".join(read_conns)

        rel_clean = re.sub(r"[^a-zA-Z0-9_]", "_", rel_out.lstrip("\\"))
        rel_lsb = outputs[rel_out].get("lsb", 0)
        signed_calc = f"""
        for (b_idx = 0; b_idx < {rel_width}; b_idx = b_idx + 1) begin
          clean_exact[b_idx] = (exact_{rel_clean}[b_idx + {rel_lsb}] === 1'b1) ? 1'b1 : 1'b0;
          clean_approx[b_idx] = (approx_{rel_clean}[b_idx + {rel_lsb}] === 1'b1) ? 1'b1 : 1'b0;
        end
"""
        rel_fmt = iface.get("relaxed_format")
        fw = rel_fmt["width"] if rel_fmt else rel_width
        kind = rel_fmt["kind"] if rel_fmt else ("s" if rel_signed else "u")
        if kind == "sm" and fw < 2:
            kind = "u"
        if kind == "s":
            signed_calc += f"""
        ev = $signed(clean_exact[{fw - 1}:0]);
        av = $signed(clean_approx[{fw - 1}:0]);
"""
        elif kind == "sm":
            signed_calc += f"""
        ev = clean_exact[{fw - 1}] ? -$signed({{1'b0, clean_exact[{fw - 2}:0]}}) : $signed({{1'b0, clean_exact[{fw - 2}:0]}});
        av = clean_approx[{fw - 1}] ? -$signed({{1'b0, clean_approx[{fw - 2}:0]}}) : $signed({{1'b0, clean_approx[{fw - 2}:0]}});
"""
        else:
            signed_calc += f"""
        ev = $signed({{1'b0, clean_exact}});
        av = $signed({{1'b0, clean_approx}});
"""
        signed_calc += """
        diff = ev - av;
"""
        tb_code = f"""`timescale 1ns/1ps
module tb_eval;
{chr(10).join(inp_decls)}
{chr(10).join(out_decls)}

  integer fd, r;
  integer sample_count;
  real total_abs_error;
  real max_abs_error;
  integer error_count;
  integer b_idx;
  reg [{rel_width-1}:0] clean_exact, clean_approx;
  reg signed [{rel_width}:0] ev, av;
  real diff;

  {iface['module_name']} u_exact (
    {", ".join(inst_exact_conns)}
  );

  {approx_mod_name} u_approx (
    {", ".join(inst_approx_conns)}
  );

  initial begin
    sample_count = 0;
    total_abs_error = 0.0;
    max_abs_error = 0.0;
    error_count = 0;

    fd = $fopen("{stim_hex}", "r");
    if (fd == 0) begin
      $display("[TB ERROR] Could not open stimulus file");
      $finish;
    end

    // Skip comment line if present
    r = $fgetc(fd);
    if (r == 35) begin // '#'
      while (r != 10 && r != -1) r = $fgetc(fd);
    end else begin
      $ungetc(r, fd);
    end

    while (!$feof(fd)) begin
      r = $fscanf(fd, "{format_str}\\n", {read_args});
      if (r == {len(inputs)}) begin
        #1;
        sample_count = sample_count + 1;
        {signed_calc}
        if (diff < 0) diff = -diff;

        total_abs_error = total_abs_error + diff;
        if (diff > max_abs_error) max_abs_error = diff;
        if (diff != 0) error_count = error_count + 1;
      end
    end
    $fclose(fd);

    if (sample_count > 0) begin
      $display("SAMPLES: %0d", sample_count);
      $display("MED: %f", total_abs_error / sample_count);
      $display("WCE: %f", max_abs_error);
      $display("ER: %f", (1.0 * error_count) / sample_count);
    end
    $finish;
  end
endmodule
"""
        tb_v.write_text(tb_code, encoding="utf-8")

        # Compile and execute with iverilog
        cmd_compile = f"iverilog -g2012 -o {vvp_bin} {tb_v} {exact_vlog} {approx_renamed_v}"
        res_c = subprocess.run(cmd_compile, shell=True, executable="/bin/bash", capture_output=True, text=True)
        if res_c.returncode != 0:
            logger.debug("iverilog compile failed:\n%s", res_c.stderr)
            return {"med": 1e9, "wce": 1e9, "er": 1.0}

        cmd_run = f"vvp {vvp_bin}"
        res_r = subprocess.run(cmd_run, shell=True, executable="/bin/bash", capture_output=True, text=True)
        if res_r.returncode != 0:
            return {"med": 1e9, "wce": 1e9, "er": 1.0}

        # Parse metrics
        med = 0.0
        wce = 0.0
        er = 0.0
        for line in res_r.stdout.splitlines():
            if line.startswith("MED:"):
                med = float(line.split(":")[1].strip())
            elif line.startswith("WCE:"):
                wce = float(line.split(":")[1].strip())
            elif line.startswith("ER:"):
                er = float(line.split(":")[1].strip())

        return {"med": med, "wce": wce, "er": er}
