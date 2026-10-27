"""Design Re-integrator.

Stitches the extracted parent shell (top_shell.v) and the approximated
submodule gate netlist (approx_cone.v) into a unified netlist (final_approx_top.v)
ready for physical design and verification.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from pyosys import libyosys as ys

logger = logging.getLogger("reintegrator")

TECH_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "third_party" / "AxLS" / "templates"
TECH_V_DEFAULT = TECH_TEMPLATES_DIR / "NanGate15nm.v"


class Reintegrator:
    """Re-integrates parent wrapper shell with approximated subcircuits."""

    def __init__(
        self,
        top_shell_path: str | Path,
        approx_cone_path: str | Path,
        extracted_cone_path: Optional[str | Path] = None,
        tech: str = "NanGate15nm",
        tech_verilog_path: Optional[str | Path] = None,
    ):
        self.top_shell_path = Path(top_shell_path).resolve()
        self.approx_cone_path = Path(approx_cone_path).resolve()
        self.extracted_cone_path = Path(extracted_cone_path).resolve() if extracted_cone_path else None

        if tech_verilog_path:
            self.tech_v = Path(tech_verilog_path).resolve()
        else:
            # Check if tech is a direct file path or library name
            tech_as_path = Path(tech)
            if tech_as_path.is_file():
                self.tech_v = tech_as_path.resolve()
            else:
                candidate = TECH_TEMPLATES_DIR / f"{tech}.v"
                self.tech_v = candidate.resolve() if candidate.is_file() else TECH_V_DEFAULT

        if not self.tech_v.exists():
            logger.warning("Technology Verilog file not found: %s", self.tech_v)

    def reintegrate(self, out_final_path: str | Path, flatten: bool = False) -> Dict[str, Any]:
        """Combine top shell and approximate submodule into unified Verilog."""
        out_p = Path(out_final_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        design = ys.Design()

        extra_submods_cmd = ""
        temp_extra_path: Optional[Path] = None
        if self.extracted_cone_path and self.extracted_cone_path.exists():
            try:
                d_approx = ys.Design()
                ys.run_pass(f"read_verilog -sv {self.approx_cone_path}", d_approx)
                approx_mods = set(m.str().lstrip("\\") for m in d_approx.modules_.keys())

                d_extracted = ys.Design()
                ys.run_pass(f"read_verilog -sv {self.extracted_cone_path}", d_extracted)
                extracted_mods = set(m.str().lstrip("\\") for m in d_extracted.modules_.keys())

                missing_mods = extracted_mods - approx_mods
                if missing_mods:
                    temp_extra_path = out_p.parent / f".tmp_extra_submods_{out_p.stem}.v"
                    sel_expr = " ".join(f"\\{m}" for m in sorted(missing_mods))
                    ys.run_pass(f"select {sel_expr}; write_verilog -selected {temp_extra_path}; select -clear", d_extracted)
                    if temp_extra_path.exists():
                        extra_submods_cmd = f"read_verilog -sv {temp_extra_path}; "
                        logger.info("Preserving unapproximated sister cone(s) in reintegration: %s", sorted(list(missing_mods)))
            except Exception as exc:
                logger.warning("Could not extract auxiliary submodules from %s: %s", self.extracted_cone_path, exc)

        try:
            tech_cmd = f"read_verilog {self.tech_v}; " if self.tech_v.exists() else ""
            flatten_cmd = "flatten; opt_clean; " if flatten else ""
            cmd = (
                f"read_verilog {self.top_shell_path} {self.approx_cone_path}; "
                f"{extra_submods_cmd}"
                f"{tech_cmd}"
                f"hierarchy -check -auto-top; "
                f"proc; opt_clean; "
                f"{flatten_cmd}"
                f"write_verilog {out_p}"
            )

            logger.info("Executing Yosys re-integration: %s", cmd)
            ys.run_pass(cmd, design)
        finally:
            if temp_extra_path and temp_extra_path.exists():
                try:
                    temp_extra_path.unlink()
                except OSError:
                    pass

        top_mod = design.top_module()
        top_name = top_mod.name.str().lstrip("\\") if top_mod else "unknown"

        return {
            "status": "SUCCESS",
            "top_module": top_name,
            "final_netlist": str(out_p),
            "flattened": flatten,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Axc Design Re-integrator")
    parser.add_argument("--top-shell", required=True, help="Path to top_shell.v")
    parser.add_argument("--approx-cone", required=True, help="Path to approx_cone.v")
    parser.add_argument("--extracted-cone", default=None, help="Path to extracted_cone.v (optional fallback for multi-cone partitions)")
    parser.add_argument("-t", "--tech", default="NanGate15nm", help="Standard cell tech library name")
    parser.add_argument("--tech-v", default=None, help="Path to standard cell Verilog library")
    parser.add_argument("-o", "--output", required=True, help="Path for final_approx_top.v")
    parser.add_argument("--flatten", action="store_true", help="Flatten hierarchy into a single module")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    reintegrator = Reintegrator(
        top_shell_path=args.top_shell,
        approx_cone_path=args.approx_cone,
        extracted_cone_path=args.extracted_cone,
        tech=args.tech,
        tech_verilog_path=args.tech_v,
    )
    result = reintegrator.reintegrate(out_final_path=args.output, flatten=args.flatten)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
