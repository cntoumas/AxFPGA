"""AxFPGA: Attribute-Driven Approximate Logic Synthesis & Verification Framework for FPGAs.

Optimized natively for Xilinx 7-Series / Artix-7 Architectures.
Integrates:
- pyALS / pyALSlib / pyALS-lut-catalog (Salvatore Barone): Catalog-based AIG-rewriting Approximate Engine.
- Broken Array Multiplier (BAM) & Partial Product Truncation.
- Lower-Part OR Adder (LOA) with Speculative Carry Generation.
- Native 6-LUT / 4-LUT cut pruning and Xilinx 7-Series resource modeling.
- Attribute-driven taint isolation and formal SAT verification.
"""

from axfpga.backend import FpgaBackend
from axfpga.budgeter import CascadedBudgeter
from axfpga.cone_extractor import ConeExtractor
from axfpga.estimator import FpgaEstimator
from axfpga.flow import AxcFlow
from axfpga.pyals_bridge import PyAlsBridge
from axfpga.reintegrator import Reintegrator
from axfpga.sdc_generator import SdcGenerator
from axfpga.sweep import ParetoSweeper
from axfpga.trace_extractor import TraceExtractor
from axfpga.verifier import MiterVerifier

# Aliases
AxcFpgaFlow = AxcFlow
MultiStageBudgeter = CascadedBudgeter

__version__ = "0.2.0"
__all__ = [
    "AxcFlow",
    "AxcFpgaFlow",
    "FpgaBackend",
    "FpgaEstimator",
    "PyAlsBridge",
    "ConeExtractor",
    "Reintegrator",
    "SdcGenerator",
    "MiterVerifier",
    "TraceExtractor",
    "ParetoSweeper",
    "MultiStageBudgeter",
]
