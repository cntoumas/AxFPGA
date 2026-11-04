"""Quality specification: number formats, metrics, stimulus sources, plugins. Pure Python + numpy, no pyosys."""

from .errors import QualityError
from .formats import NumberFormat
from .metrics import ERROR_METRICS, REGISTRY, Metric, compute, decoded, error_stats, psnr_db, sqnr_db
from .plugin import Context, Plugin, Stimulus, load_plugin
from .spec import Constraint, QualitySpec
from .stimulus import (SEED_HELDOUT, SEED_TUNING, derive_seed, load_hex, power_cap, random_vectors, resolve_path,
                       split_file_vectors)

__all__ = [
    "Constraint", "QualitySpec", "QualityEvaluator", "make_evaluator", "WireQuality", "parse_wire_attrs",
    "QualityError", "NumberFormat", "ERROR_METRICS", "REGISTRY", "Metric", "compute", "decoded", "error_stats",
    "psnr_db", "sqnr_db", "Context", "Plugin", "Stimulus", "load_plugin", "SEED_HELDOUT", "SEED_TUNING",
    "derive_seed", "load_hex", "power_cap", "random_vectors", "resolve_path", "split_file_vectors",
]


def __getattr__(name):
    # evaluator/attrs import lazily: they need nothing heavy, but keep package import cheap and cycle-free
    if name in ("QualityEvaluator", "make_evaluator"):
        from . import evaluator
        return getattr(evaluator, name)
    if name in ("WireQuality", "parse_wire_attrs"):
        from . import attrs
        return getattr(attrs, name)
    raise AttributeError(name)
