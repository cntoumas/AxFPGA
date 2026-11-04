"""User plugin loader: a .py file providing stimulus(stream, ctx), metric(stream, vectors, exact, approx, ctx), power_stimulus(vectors, ctx)."""

from __future__ import annotations

import hashlib
import importlib.util
import numbers
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import QualityError
from .formats import NumberFormat
from .stimulus import STREAMS


@dataclass
class Context:
    design_id: str = ""
    top: str = ""
    relax_wire: str = ""
    width: int = 0
    inputs: List[Tuple[str, int]] = field(default_factory=list)
    outputs: List[Tuple[str, int]] = field(default_factory=list)
    fmt: Optional[NumberFormat] = None
    seed: int = 1
    samples: int = 1000
    skip: int = 0
    has_clock: bool = False
    attrs: Dict[str, Any] = field(default_factory=dict)
    base_dir: Path = Path(".")
    search_dirs: List[Path] = field(default_factory=list)


@dataclass
class Stimulus:
    vectors: List[Dict[str, int]]
    note: str = ""


def _find(p: str, base_dir: Path, search_dirs: Sequence[Path]) -> Path:
    q = Path(p)
    dirs = [Path(base_dir), *[Path(d) for d in search_dirs]]
    cands = [q] if q.is_absolute() else [d / q for d in dirs]
    for c in cands:
        if c.is_file():
            return c
    raise QualityError(f"axc_plugin '{p}': file not found (looked in: {', '.join(str(c.parent) for c in cands)})")


class Plugin:
    def __init__(self, path: Path, module):
        self.path, self._m = path, module

    def _fn(self, name: str):
        f = getattr(self._m, name, None)
        return f if callable(f) else None

    def has_stimulus(self) -> bool:
        return self._fn("stimulus") is not None

    def has_metric(self) -> bool:
        return self._fn("metric") is not None

    def has_power_stimulus(self) -> bool:
        return self._fn("power_stimulus") is not None

    def _err(self, msg: str) -> QualityError:
        return QualityError(f"axc_plugin '{self.path.name}': {msg}")

    @staticmethod
    def _check_stream(stream: str):
        if stream not in STREAMS:
            raise QualityError(f"unknown stream '{stream}' (expected 'tuning' or 'heldout')")

    def _vectors(self, what: str, res) -> List[Dict[str, int]]:
        if not isinstance(res, (list, tuple)) or not res:
            raise self._err(f"{what} must return a non-empty list of {{port: int}} dicts")
        for i, v in enumerate(res):
            if not isinstance(v, Mapping) or not all(isinstance(k, str) and isinstance(x, numbers.Integral) for k, x in v.items()):
                raise self._err(f"{what}: vector {i} is not a {{port: int}} dict")
        return [{k: int(x) for k, x in v.items()} for v in res]

    def stimulus(self, stream: str, ctx: Context) -> Stimulus:
        self._check_stream(stream)
        f = self._fn("stimulus")
        if f is None:
            raise self._err("provides no stimulus()")
        try:
            res = f(stream, ctx)
        except QualityError:
            raise
        except Exception as e:
            raise self._err(f"stimulus() failed: {type(e).__name__}: {e}") from e
        if isinstance(res, Stimulus):
            return Stimulus(self._vectors("stimulus()", res.vectors), res.note)
        return Stimulus(self._vectors("stimulus()", res), "")

    def metric(self, stream: str, vectors, exact, approx, ctx: Context) -> Dict[str, Any]:
        self._check_stream(stream)
        f = self._fn("metric")
        if f is None:
            raise self._err("provides no metric()")
        try:
            res = f(stream, vectors, exact, approx, ctx)
        except QualityError:
            raise
        except Exception as e:
            raise self._err(f"metric() failed: {type(e).__name__}: {e}") from e
        if not isinstance(res, Mapping):
            raise self._err("metric() must return a dict with name, value, unit")
        missing = [k for k in ("name", "value", "unit") if k not in res]
        if missing:
            raise self._err(f"metric() result lacks {', '.join(missing)}")
        if not isinstance(res["value"], numbers.Real) or isinstance(res["value"], bool):
            raise self._err("metric() 'value' must be a number")
        out = dict(res)
        out["value"] = float(out["value"])
        out["name"], out["unit"] = str(out["name"]), str(out["unit"])
        out.setdefault("extra", {})
        return out

    def power_stimulus(self, vectors, ctx: Context) -> List[Dict[str, int]]:
        f = self._fn("power_stimulus")
        if f is None:
            raise self._err("provides no power_stimulus()")
        try:
            res = f(vectors, ctx)
        except QualityError:
            raise
        except Exception as e:
            raise self._err(f"power_stimulus() failed: {type(e).__name__}: {e}") from e
        return self._vectors("power_stimulus()", res)


def load_plugin(path_or_name: str, base_dir: Path, search_dirs: Sequence[Path] = ()) -> Plugin:
    """Import a plugin file under a unique module name; sys.path is not changed."""
    path = _find(path_or_name, base_dir, search_dirs).resolve()
    name = "axfpga_plugin_%s_%s" % (path.stem, hashlib.sha1(str(path).encode()).hexdigest()[:8])
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        raise QualityError(f"axc_plugin '{path_or_name}': {type(e).__name__}: {e}") from e
    p = Plugin(path, mod)
    if not (p.has_stimulus() or p.has_metric() or p.has_power_stimulus()):
        raise QualityError(f"axc_plugin '{path_or_name}': defines none of stimulus(), metric(), power_stimulus()")
    return p
