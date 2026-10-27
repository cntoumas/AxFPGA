"""Attribute-Driven SDC Timing Constraint Generator for Approximate Circuits.

Emits relaxed timing constraints (multicycle paths or relaxed set_max_delay)
specifically on (* axc_relax *) output ports/bits so OpenROAD and OpenSTA
avoid aggressive buffer insertion and area inflation on approximate datapath bits.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sdc_generator")


class SdcGenerator:
    """Generates relaxed SDC timing constraints based on AxC metadata."""

    def __init__(
        self,
        clk_port: str = "clk",
        clk_period_ns: float = 10.0,
        relaxation_factor: float = 1.5,
        constraint_mode: str = "max_delay",  # "max_delay" or "multicycle"
    ):
        self.clk_port = clk_port
        self.clk_period_ns = clk_period_ns
        self.relaxation_factor = relaxation_factor
        self.constraint_mode = constraint_mode

    def generate_sdc(
        self,
        metadata: Dict[str, Any],
        out_sdc_path: str | Path,
    ) -> str:
        """Emit SDC file based on relaxed signals metadata."""
        out_p = Path(out_sdc_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        lines: List[str] = [
            "# =====================================================================",
            "# Auto-generated Attribute-Driven SDC Timing Constraints",
            f"# Top Module: {metadata.get('top_module', 'top')}",
            f"# Clock: {self.clk_port} @ {self.clk_period_ns:.2f} ns",
            "# =====================================================================",
            "",
            "# 1. Primary Clock Definition",
            f"create_clock -name {self.clk_port} -period {self.clk_period_ns:.3f} [get_ports {self.clk_port}]",
            f"set_clock_uncertainty 0.05 [get_clocks {self.clk_port}]",
            "",
            "# 2. Default I/O Timing Delays (30% input, 30% output budgets)",
            f"set_input_delay [expr {{{self.clk_period_ns:.3f} * 0.30}}] -clock {self.clk_port} [all_inputs -no_clocks]",
            f"set_output_delay [expr {{{self.clk_period_ns:.3f} * 0.30}}] -clock {self.clk_port} [all_outputs]",
            "",
            "# 3. Attribute-Driven Approximate Timing Relaxation",
        ]

        relaxed_signals = metadata.get("relaxed_signals", [])
        if not relaxed_signals:
            lines.append("# No (* axc_relax *) signals found. Standard precise constraints applied.")
        else:
            relaxed_delay = self.clk_period_ns * self.relaxation_factor
            for sig in relaxed_signals:
                wire_name = sig["wire"]
                lsb_width = sig.get("lsb_width", sig.get("width", 1))
                width = sig.get("width", 1)

                lines.append(f"# Relaxed Output Signal: {wire_name} (width={width}, relaxed_lsbs={lsb_width})")
                lines.append(f"# Metric: {sig.get('metric', 'MED')} | Bound: {sig.get('bound', '0.0')}")

                for bit_idx in range(lsb_width):
                    port_target = f"[get_ports {{{wire_name}[{bit_idx}]}}]" if width > 1 else f"[get_ports {{{wire_name}}}]"
                    if self.constraint_mode == "multicycle":
                        lines.append(f"set_multicycle_path 2 -setup -to {port_target}")
                        lines.append(f"set_multicycle_path 1 -hold -to {port_target}")
                    else:
                        lines.append(f"set_max_delay {relaxed_delay:.3f} -to {port_target}")
                lines.append("")

        sdc_content = "\n".join(lines) + "\n"
        out_p.write_text(sdc_content, encoding="utf-8")
        logger.info("SDC written to %s (%d lines)", out_p, len(lines))
        return sdc_content


def main() -> None:
    parser = argparse.ArgumentParser(description="Attribute-Driven SDC Generator")
    parser.add_argument("-m", "--meta", required=True, help="Path to metadata.json from cone_extractor")
    parser.add_argument("-o", "--output", required=True, help="Path for output .sdc file")
    parser.add_argument("--clk", default="clk", help="Clock port name (default: clk)")
    parser.add_argument("--period", type=float, default=10.0, help="Clock period in ns (default: 10.0)")
    parser.add_argument("--factor", type=float, default=1.5, help="Timing relaxation factor (default: 1.5)")
    parser.add_argument("--mode", choices=["max_delay", "multicycle"], default="max_delay", help="Constraint mode")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    with open(args.meta, "r", encoding="utf-8") as f:
        meta = json.load(f)

    gen = SdcGenerator(
        clk_port=args.clk,
        clk_period_ns=args.period,
        relaxation_factor=args.factor,
        constraint_mode=args.mode,
    )
    gen.generate_sdc(metadata=meta, out_sdc_path=args.output)
    print(f"Generated SDC at {args.output}")


if __name__ == "__main__":
    main()
