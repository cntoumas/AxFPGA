"""Central configuration: tool locations and external data paths."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import List


def _pyosys_dir() -> Path:
    spec = importlib.util.find_spec("pyosys")
    if spec is None or spec.origin is None:
        raise RuntimeError("pyosys is not installed; install AxFPGA with its dependencies (pyosys==0.70)")
    return Path(spec.origin).resolve().parent


def yosys_cmd() -> List[str]:
    """argv prefix that runs the pinned Yosys command line (``-p``, ``-s``, ``-q`` supported)."""
    exe = os.environ.get("AXFPGA_YOSYS")
    if exe:
        return [exe]
    return [sys.executable, "-m", "axfpga.yosys_cli"]


def yosys_share_dir() -> Path:
    """The Yosys ``share`` directory of the pinned version (what ``+/`` refers to in scripts)."""
    return _pyosys_dir() / "share"


def xilinx_cells_sim() -> Path:
    """Simulation models of the Xilinx primitives (LUTn, CARRY4, FDRE, ...) of the pinned Yosys."""
    return yosys_share_dir() / "xilinx" / "cells_sim.v"


def yosys_abc() -> Path:
    """The ``yosys-abc`` binary bundled with the pinned Yosys."""
    return _pyosys_dir() / ("yosys-abc.exe" if os.name == "nt" else "yosys-abc")


def axbench_root() -> Path:
    """Root of the AxBench Verilog checkout (contains ``circuits/`` and ``scripts/``)."""
    return Path(os.environ.get("AXFPGA_AXBENCH_ROOT", Path.home() / "Downloads" / "axbench-verilog")).expanduser().resolve()


def vivado_bin() -> str:
    """Vivado executable, used only for validation."""
    return os.environ.get("AXFPGA_VIVADO", "vivado")
