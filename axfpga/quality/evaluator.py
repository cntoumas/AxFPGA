"""QualityEvaluator: the pipeline's evaluator duck type, driven by a WireQuality (attributes) and an optional plugin.

Tuning stream = the legacy stream (seed unchanged, first half of a file, plugin stimulus("tuning")). The held-out
stream (derived seed, second half of a file, plugin stimulus("heldout")) is evaluated once on the accepted design
and never feeds back into selection.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .attrs import WireQuality
from .errors import QualityError
from .formats import NumberFormat
from .metrics import REGISTRY, compute, decoded, error_stats as _decoded_error_stats
from .plugin import Context, Plugin, load_plugin
from .spec import QualitySpec
from .stimulus import SEED_HELDOUT, SEED_TUNING, load_hex, power_cap, random_vectors, resolve_path, split_file_vectors

logger = logging.getLogger("axfpga.quality.evaluator")


class QualityEvaluator:
    def __init__(self, wq: WireQuality, inputs: Sequence[Tuple[str, int]], ctx: Context, plugin: Optional[Plugin] = None,
                 quality: Optional[QualitySpec] = None, signed: bool = False, n_vectors: int = 1000):
        self.wq, self.inputs, self.ctx, self.plugin = wq, list(inputs), ctx, plugin
        self._quality = quality or wq.quality
        self.signed = signed
        self.n_vectors = n_vectors
        self.width = wq.width
        self.legacy_modular = wq.fmt is None
        self.fmt = wq.fmt or NumberFormat("s" if signed else "u").bind(wq.width, wq.wire)
        self._file_cache: Optional[Tuple[List[dict], List[dict]]] = None
        if plugin is not None and wq.stimulus and plugin.has_stimulus():
            logger.warning("'%s': axc_stimulus file and plugin stimulus() both given; the file is used", wq.wire)

    # ------------------------------------------------------------------ spec
    @property
    def quality(self) -> QualitySpec:
        if self._quality is None:
            raise QualityError(f"no quality constraints for '{self.wq.wire}'")
        return self._quality

    # ------------------------------------------------------------------ stimulus
    def _file_halves(self) -> Tuple[List[dict], List[dict]]:
        if self._file_cache is None:
            pre = f"axc_stimulus of '{self.wq.wire}': "
            try:
                p = resolve_path(self.wq.stimulus, self.ctx.base_dir, [*self.ctx.search_dirs, Path.cwd()])
                self._file_cache = split_file_vectors(load_hex(p, self.inputs))
            except QualityError as e:
                raise QualityError(pre + str(e)) from None
        return self._file_cache

    def _stimulus(self, stream: str) -> Tuple[List[dict], Dict[str, Any]]:
        if self.wq.stimulus:
            tune, held = self._file_halves()
            vecs = tune if stream == SEED_TUNING else held
            return vecs, {"note": f"{self.wq.stimulus} ({'first' if stream == SEED_TUNING else 'second'} half, {len(vecs)} vectors)"}
        if self.plugin is not None and self.plugin.has_stimulus():
            st = self.plugin.stimulus(stream, self.ctx)
            return st.vectors, {"note": st.note}
        return random_vectors(self.inputs, self.n_vectors, self.wq.seed, stream)

    def build_stimulus(self, bench_id: str = "") -> Tuple[List[Dict[str, int]], Dict[str, Any]]:
        return self._stimulus(SEED_TUNING)

    def build_heldout(self, bench_id: str = "") -> Optional[Tuple[List[Dict[str, int]], Dict[str, Any]]]:
        return self._stimulus(SEED_HELDOUT)

    def power_vectors(self, bench_id: str, vectors: List[Dict[str, int]]) -> List[Dict[str, int]]:
        if self.plugin is not None and self.plugin.has_power_stimulus():
            return self.plugin.power_stimulus(vectors, self.ctx)
        return power_cap(vectors)

    # ------------------------------------------------------------------ errors
    def _modular(self, exact, approx, wire, skip) -> Dict[str, float]:
        """Phase-1 GenericEvaluator error: modular distance of the two words (no explicit axc_format)."""
        mod, half = 1 << self.width, 1 << (self.width - 1)
        n = min(len(exact), len(approx))
        errs, mreds = [], []
        for e_row, a_row in zip(exact[skip:n], approx[skip:n]):
            e, a = e_row[wire] % mod, a_row[wire] % mod
            d = (e - a) % mod
            d = min(d, mod - d)
            ref = abs(e - mod if (self.signed and e >= half) else e)
            errs.append(d)
            if ref:
                mreds.append(d / ref)
        k = len(errs)
        return {"wce": float(max(errs)) if k else 0.0, "med": sum(errs) / k if k else 0.0,
                "mred": sum(mreds) / len(mreds) if mreds else 0.0, "er": sum(1 for d in errs if d) / k if k else 0.0,
                "samples": k, "format": "signed" if self.signed else "unsigned"}

    def error_stats(self, bench_id: str, exact, approx, wire: str, skip: int) -> Dict[str, float]:
        out = (self._modular(exact, approx, wire, skip) if self.legacy_modular
               else _decoded_error_stats(self.fmt, exact, approx, wire, skip))
        out.update(self._constrained_extras(exact, approx, wire, skip))
        return out

    def _constrained_extras(self, exact, approx, wire: str, skip: int) -> Dict[str, float]:
        """rmse and the higher-is-better built-ins, when the spec constrains them (with the constraint's options)."""
        q = self._quality
        if q is None:
            return {}
        cs = [c for c in q.constraints if c.metric == "rmse"] + q.builtin_higher()
        if not cs:
            return {}
        n = min(len(exact), len(approx))
        e, a = decoded(self.fmt, exact, wire, skip, n), decoded(self.fmt, approx, wire, skip, n)
        out = {}
        for c in cs:
            opts = dict(c.opts)
            if c.metric == "psnr":
                opts.setdefault("peak", self.fmt.peak)
            out[c.metric] = compute(c.metric, e, a, opts)
        return out

    # ------------------------------------------------------------------ application metric
    def app_metrics(self, bench_id: str, vectors, exact, approx, relax_wire: str = "", skip: int = 0) -> Dict[str, Any]:
        return self.app_metrics_for(SEED_TUNING, vectors, exact, approx, skip)

    def app_metrics_for(self, stream: str, vectors, exact, approx, skip: int = 0) -> Dict[str, Any]:
        q = self._quality
        wire = self.wq.wire
        if self.plugin is not None and self.plugin.has_metric():
            app = self.plugin.metric(stream, vectors, exact, approx, dataclasses.replace(self.ctx, skip=skip))
        elif q is not None and q.builtin_higher():
            c = q.builtin_higher()[0]
            n = min(len(exact), len(approx))
            e, a = decoded(self.fmt, exact, wire, skip, n), decoded(self.fmt, approx, wire, skip, n)
            opts = dict(c.opts)
            if c.metric == "psnr":
                opts.setdefault("peak", self.fmt.peak)
            m = REGISTRY[c.metric]
            app = {"name": m.label, "value": compute(c.metric, e, a, opts), "unit": m.unit, "extra": {}}
            app["threshold"] = f"{c.op} {c.threshold!r} {m.unit}"
            app["pass"] = bool(c.holds(app["value"]))
        else:
            return {"name": "none", "value": None, "unit": "", "threshold": None, "pass": None}
        app.setdefault("threshold", None)
        app.setdefault("pass", None)
        if q is not None:
            q.apply_app(app)
        return app


def make_evaluator(wq: WireQuality, *, design_id: str, top: str, inputs: Sequence[Tuple[str, int]],
                   outputs: Sequence[Tuple[str, int]], has_clock: bool, base_dir: Path, search_dirs: Sequence[Path] = (),
                   default_samples: int = 1000, signed: bool = False, quality: Optional[QualitySpec] = None,
                   raw_attrs: Optional[Mapping[str, Any]] = None) -> QualityEvaluator:
    """Evaluator for one relaxed wire: loads the plugin named by axc_plugin and builds the plugin Context."""
    n = max(wq.samples if wq.samples is not None else default_samples, 64)
    fmt = wq.fmt
    ctx = Context(design_id=design_id, top=top, relax_wire=wq.wire, width=wq.width, inputs=list(inputs),
                  outputs=list(outputs), fmt=fmt, seed=wq.seed, samples=n, skip=16 if has_clock else 0,
                  has_clock=has_clock, attrs=dict(raw_attrs or {}), base_dir=Path(base_dir),
                  search_dirs=[Path(d) for d in search_dirs])
    plugin = load_plugin(wq.plugin, Path(base_dir), [*map(Path, search_dirs), Path.cwd()]) if wq.plugin else None
    return QualityEvaluator(wq, inputs, ctx, plugin, quality=quality, signed=signed, n_vectors=n)
