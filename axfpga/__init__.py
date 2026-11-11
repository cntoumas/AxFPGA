"""AxFPGA: Attribute-Driven Approximate Logic Synthesis & Verification Framework for FPGAs."""

from axfpga.alu_splitter import AluSplitter
from axfpga.budgeter import CascadedBudgeter
from axfpga.cone_extractor import ConeExtractor
from axfpga.estimator import FpgaEstimator
from axfpga.flow import AxcFlow
from axfpga.ppg_pruner import PpgPruner
from axfpga.pyals_bridge import PyAlsBridge
from axfpga.reintegrator import Reintegrator
from axfpga.sdc_generator import SdcGenerator
from axfpga.sweep import ParetoSweeper
from axfpga.tournament import Champion, Tournament
from axfpga.trace_extractor import TraceExtractor
from axfpga.verifier import MiterVerifier
from axfpga.vivado import VivadoBatchRunner

# Aliases
AxcFpgaFlow = AxcFlow
MultiStageBudgeter = CascadedBudgeter

__version__ = "0.2.0"
__all__ = [
    "AluSplitter",
    "AxcFlow",
    "AxcFpgaFlow",
    "FpgaEstimator",
    "PpgPruner",
    "PyAlsBridge",
    "ConeExtractor",
    "Reintegrator",
    "SdcGenerator",
    "MiterVerifier",
    "TraceExtractor",
    "ParetoSweeper",
    "Champion",
    "Tournament",
    "MultiStageBudgeter",
    "VivadoBatchRunner",
]
