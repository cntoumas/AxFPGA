"""Cone Interface parsing.

Reads the module name, ports, bus ranges and relaxed output of an extracted
Cone netlist. Shared by the Tournament and every candidate Generator so they
agree on which output is the relaxed one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional


def parse_cone_interface(cone_vlog: str | Path, meta_json: Optional[str | Path] = None) -> Dict[str, Any]:
    """Extract module name, ports, widths, and relaxed signal info from a Cone netlist."""
    cone_vlog = Path(cone_vlog)
    content = cone_vlog.read_text(encoding="utf-8")
    mod_m = re.search(r"module\s+(\w+)\s*\((.*?)\);", content, re.DOTALL)
    if not mod_m:
        raise ValueError(f"Could not parse module header from {cone_vlog}")

    mod_name = mod_m.group(1)
    raw_ports = [p.strip() for p in mod_m.group(2).split(",") if p.strip()]

    # Scope content strictly to the target module to avoid bleeding ports from sister modules
    end_idx = content.find("endmodule")
    mod_content = content[:end_idx + len("endmodule")] if end_idx != -1 else content

    inputs: Dict[str, Dict[str, Any]] = {}
    outputs: Dict[str, Dict[str, Any]] = {}

    port_pat = re.compile(r"\b(input|output|inout)\s+(?:wire\s+|reg\s+)?(?:signed\s+)?(?:\[([^\]]+)\]\s*)?(\S+)")
    for line in mod_content.splitlines():
        line = line.strip()
        m = port_pat.search(line)
        if m:
            direction = m.group(1)
            width_str = m.group(2)
            name = m.group(3).rstrip(";").strip()
            width = 1
            msb = 0
            lsb = 0
            if width_str:
                w_m = re.match(r"\[?(\d+):(\d+)\]?", width_str.strip())
                if w_m:
                    msb = int(w_m.group(1))
                    lsb = int(w_m.group(2))
                    width = abs(msb - lsb) + 1
            info = {"name": name, "width": width, "msb": msb, "lsb": lsb, "signed": False}
            if direction in ("input", "inout"):
                inputs[name] = info
            if direction in ("output", "inout"):
                outputs[name] = info

    # Select relaxed output: the annotated (* axc_relax *) wire if it is a Cone output,
    # else the widest multi-bit output (the main arithmetic bus)
    meta_wire = None
    meta_format = None
    if meta_json and Path(meta_json).exists():
        try:
            meta = json.loads(Path(meta_json).read_text(encoding="utf-8"))
            meta_wire = next((s.get("wire") for s in meta.get("relaxed_signals", []) if s.get("wire") in outputs), None)
            rel_sigs = meta.get("relaxed_signals", [])
            meta_format = next((s.get("quality", {}).get("format") for s in rel_sigs if s.get("quality", {}).get("format")), None)
        except (OSError, ValueError):
            meta_wire = None

    multi_outs = [k for k, v in outputs.items() if v["width"] > 1]
    if meta_wire:
        relaxed_out = meta_wire
    elif multi_outs:
        relaxed_out = max(multi_outs, key=lambda k: outputs[k]["width"])
    elif outputs:
        relaxed_out = list(outputs.keys())[-1]
    elif inputs:
        relaxed_out = list(inputs.keys())[-1]
    else:
        relaxed_out = "out"

    # Signedness of the relaxed output, detected from signed declarations in the Cone
    if relaxed_out in outputs and any(
        tok in mod_content for tok in ("$signed", "wire signed", "output signed", "output reg signed")
    ):
        outputs[relaxed_out]["signed"] = True

    # Explicit number format of the relaxed output (axc_format): {"kind": "u"|"s"|"sm", "width": W, "frac": F}, else None.
    # With it the Tournament measures the error on the decoded value, like the application stream does; without it
    # the legacy raw-word distance is kept.
    relaxed_format = None
    if meta_format and relaxed_out in outputs:
        try:
            from axfpga.quality.formats import NumberFormat
            nf = NumberFormat.parse(meta_format).bind(outputs[relaxed_out]["width"])
            relaxed_format = {"kind": nf.kind, "width": nf.width, "frac": nf.frac}
        except ValueError:
            relaxed_format = None

    return {
        "module_name": mod_name,
        "ports": raw_ports,
        "inputs": inputs,
        "outputs": outputs,
        "relaxed_output": relaxed_out,
        "relaxed_format": relaxed_format,
        "raw_content": mod_content,
    }
