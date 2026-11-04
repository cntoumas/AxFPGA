"""Metric registry. Formulas are copies of scripts/axbench_apps.py (error_stats, _sqnr_db, _psnr_db, app_metrics)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence

import numpy as np

from .formats import NumberFormat

Opts = Mapping[str, float]


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    higher_is_better: bool
    unit: str                 # "" | "dB" | "%"
    ratio: bool               # accepts the % suffix in the grammar
    fn: Callable[[np.ndarray, np.ndarray, Opts], float]
    options: Sequence[str] = ()


def sqnr_db(e: np.ndarray, a: np.ndarray) -> float:
    err = np.sum((e - a) ** 2, dtype=np.float64)
    sig = np.sum(e.astype(np.float64) ** 2)
    if err == 0:
        return float("inf")
    return 10.0 * math.log10(sig / err) if sig > 0 else float("-inf")


def psnr_db(e: np.ndarray, a: np.ndarray, peak: float) -> float:
    mse = np.mean((e.astype(np.float64) - a) ** 2)
    return float("inf") if mse == 0 else 10.0 * math.log10(peak * peak / mse)


def _wce(e, a, o):
    d = np.abs(e - a)
    return float(d.max()) if d.size else 0.0


def _med(e, a, o):
    d = np.abs(e - a)
    return float(d.mean()) if d.size else 0.0


def _mred(e, a, o):
    d = np.abs(e - a)
    nz = e != 0
    return float((d[nz] / np.abs(e[nz])).mean()) if nz.any() else 0.0


def _er(e, a, o):
    d = np.abs(e - a)
    return float((d > 0).mean()) if d.size else 0.0


def _rmse(e, a, o):
    d = np.abs(e - a)
    return float(np.sqrt((d ** 2).mean())) if d.size else 0.0


def _psnr(e, a, o):
    return psnr_db(e, a, float(o["peak"]))


def _agreement(e, a, o):
    thr = float(o.get("thr", 0.0))
    return float(((e >= thr) == (a >= thr)).mean()) if e.size else 0.0


REGISTRY: Dict[str, Metric] = {m.name: m for m in (
    Metric("wce", "WCE", False, "", False, _wce),
    Metric("med", "MED", False, "", False, _med),
    Metric("mred", "MRED", False, "", True, _mred),
    Metric("er", "ER", False, "", True, _er),
    Metric("rmse", "RMSE", False, "", False, _rmse),
    Metric("sqnr", "SQNR", True, "dB", False, lambda e, a, o: sqnr_db(e, a)),
    Metric("snr", "SNR", True, "dB", False, lambda e, a, o: sqnr_db(e, a)),
    Metric("psnr", "PSNR", True, "dB", False, _psnr, ("peak",)),
    Metric("agreement", "AGREEMENT", True, "%", True, _agreement, ("thr",)),
)}
ERROR_METRICS = ("wce", "med", "mred", "er", "rmse")


def decoded(fmt: NumberFormat, rows: Sequence[Mapping[str, int]], wire: str, skip: int, n: Optional[int] = None) -> np.ndarray:
    """Decoded float64 column rows[skip:n][wire] (object -> float64, so words wider than 63 bits work)."""
    sel = rows[skip:n] if n is not None else rows[skip:]
    return np.array([fmt.decode(int(r[wire])) for r in sel], dtype=object).astype(np.float64)


def compute(name: str, e: np.ndarray, a: np.ndarray, opts: Optional[Opts] = None) -> float:
    return REGISTRY[name].fn(e, a, dict(opts or {}))


def error_stats(fmt: NumberFormat, exact_rows, approx_rows, wire: str, skip: int, extra: Sequence[str] = ()) -> Dict[str, float]:
    """wce/med/mred/er/samples/format of one output on the decoded values, plus the requested extra metrics.

    Extra metric options: psnr uses the format peak, agreement uses thr 0; per-constraint options are applied by
    QualitySpec.evaluate callers that pass them through compute().
    """
    n = min(len(exact_rows), len(approx_rows))
    e = decoded(fmt, exact_rows, wire, skip, n)
    a = decoded(fmt, approx_rows, wire, skip, n)
    out: Dict[str, float] = {
        "wce": _wce(e, a, {}), "med": _med(e, a, {}), "mred": _mred(e, a, {}), "er": _er(e, a, {}),
        "samples": int(e.size), "format": str(fmt),
    }
    for name in extra:
        if name in out or name not in REGISTRY:
            continue
        opts = {"peak": fmt.peak} if name == "psnr" else {}
        out[name] = compute(name, e, a, opts)
    return out
