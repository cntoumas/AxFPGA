"""Raw ``axc_*`` attribute dict -> validated :class:`WireQuality`."""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional

from .formats import NumberFormat
from .errors import QualityError
from .spec import QualitySpec

logger = logging.getLogger("axfpga.quality.attrs")

# Attributes owned by the cone extractor (pre-Phase-2), accepted and ignored here.
LEGACY_ATTRS = frozenset({"axc_relax", "axc_precise", "axc_metric", "axc_bound", "axc_lsb_width"})
# Phase-2 attributes on a relaxed wire that feed WireQuality.
QUALITY_ATTRS = frozenset({"axc_format", "axc_quality", "axc_stimulus", "axc_plugin",
                           "axc_seed", "axc_samples"})
# Parsed and stored, not used before Phase 5/6.
RESERVED_ATTRS = {"axc_max_sites": "max_sites", "axc_ops": "ops",
                  "axc_horizon": "horizon", "axc_clear_period": "clear_period"}
# Attributes that live on input ports.
PORT_ATTRS = frozenset({"axc_clock", "axc_reset"})

KNOWN_OPS = ("trunc", "loa", "ppg", "pyals", "lut6")
MIN_SAMPLES = 64


def known_attr_names() -> FrozenSet[str]:
    return frozenset(LEGACY_ATTRS | QUALITY_ATTRS | set(RESERVED_ATTRS) | PORT_ATTRS)


@dataclass
class WireQuality:
    wire: str
    width: int
    fmt: Optional[NumberFormat] = None            # None = auto
    quality: Optional[QualitySpec] = None
    stimulus: Optional[str] = None
    plugin: Optional[str] = None
    seed: int = 1
    samples: Optional[int] = None
    reserved: Dict[str, Any] = field(default_factory=dict)
    quality_text: Optional[str] = None            # original axc_quality text
    bound: Optional[float] = None                 # legacy axc_bound

    @property
    def wce_bound(self) -> Optional[float]:
        """Formal bound: ``wce<=`` in axc_quality wins over axc_bound."""
        if self.quality is not None and self.quality.wce_bound is not None:
            return float(self.quality.wce_bound)
        return self.bound

    def to_dict(self) -> Dict[str, Any]:
        q = None
        if self.quality is not None:
            q = [{"metric": c.metric, "op": c.op, "threshold": c.threshold,
                  "opts": {k: v for k, v in c.opts}, "text": c.text}
                 for c in self.quality.constraints]
        return {
            "wire": self.wire, "width": self.width,
            "format": str(self.fmt) if self.fmt is not None else None,
            "quality": self.quality_text, "constraints": q,
            "stimulus": self.stimulus, "plugin": self.plugin,
            "seed": self.seed, "samples": self.samples,
            "bound": self.bound, "reserved": dict(self.reserved),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "WireQuality":
        wire, width = d["wire"], int(d["width"])
        fmt = NumberFormat.parse(d["format"], wire).bind(width, wire) if d.get("format") else None
        qt = d.get("quality")
        return cls(wire=wire, width=width, fmt=fmt,
                   quality=QualitySpec.parse(qt, wire) if qt else None,
                   stimulus=d.get("stimulus"), plugin=d.get("plugin"),
                   seed=int(d.get("seed", 1)), samples=d.get("samples"),
                   reserved=dict(d.get("reserved") or {}), quality_text=qt,
                   bound=d.get("bound"))


def _as_int(raw: Mapping[str, Any], key: str, wire: str, minimum: int) -> Optional[int]:
    if key not in raw:
        return None
    v = raw[key]
    if isinstance(v, bool):
        raise QualityError(f"{key} of '{wire}': expected an integer, got {v!r}")
    try:
        n = int(v.strip(), 0) if isinstance(v, str) else int(v)
        if isinstance(v, float) and n != v:
            raise ValueError
    except (ValueError, TypeError):
        raise QualityError(f"{key} of '{wire}': expected an integer, got {v!r}") from None
    if n < minimum:
        raise QualityError(f"{key} of '{wire}': must be >= {minimum}, got {n}")
    return n


def _as_str(raw: Mapping[str, Any], key: str, wire: str) -> Optional[str]:
    if key not in raw:
        return None
    v = raw[key]
    if not isinstance(v, str) or not v.strip():
        raise QualityError(f"{key} of '{wire}': expected a non-empty string, got {v!r}")
    return v.strip()


def parse_wire_attrs(raw: Mapping[str, Any], wire: str, width: int) -> WireQuality:
    """Validate the axc_* attributes of one relaxed wire. Raises QualityError (a ValueError)."""
    known = known_attr_names()
    for key in raw:
        if key.startswith("axc_") and key not in known:
            hint = difflib.get_close_matches(key, sorted(known), n=1)
            logger.warning("unknown attribute '%s' on '%s'%s", key, wire,
                           f"; did you mean '{hint[0]}'?" if hint else "")
    for key in sorted(PORT_ATTRS & set(raw)):
        raise QualityError(f"{key} on '{wire}': belongs on an input port, not on a relaxed wire")

    wq = WireQuality(wire=wire, width=width)

    if "axc_format" in raw:
        wq.fmt = NumberFormat.parse(str(raw["axc_format"]).strip(), wire).bind(width, wire)

    if "axc_bound" in raw:
        try:
            wq.bound = float(raw["axc_bound"])
        except (ValueError, TypeError):
            raise QualityError(f"axc_bound of '{wire}' is not a number: {raw['axc_bound']!r}") from None

    wq.stimulus = _as_str(raw, "axc_stimulus", wire)
    wq.plugin = _as_str(raw, "axc_plugin", wire)
    seed = _as_int(raw, "axc_seed", wire, 0)
    wq.seed = 1 if seed is None else seed
    wq.samples = _as_int(raw, "axc_samples", wire, MIN_SAMPLES)

    qtext = _as_str(raw, "axc_quality", wire)
    if qtext is not None:
        spec = QualitySpec.parse(qtext, wire)
        wq.quality, wq.quality_text = spec, qtext
        if spec.get("app") is not None and wq.plugin is None:
            raise QualityError(f"axc_quality of '{wire}': 'app' needs an axc_plugin that provides metric()")
        if spec.wce_bound is None and wq.bound is None:
            raise QualityError(f"axc_quality of '{wire}': needs a wce<= constraint (or axc_bound): "
                               f"the formal worst-case proof needs a bound")
        if spec.wce_bound is not None and wq.bound is not None and float(spec.wce_bound) != wq.bound:
            logger.warning("'%s': wce<=%g in axc_quality overrides axc_bound=%g",
                           wire, spec.wce_bound, wq.bound)

    for key, short in RESERVED_ATTRS.items():
        if key == "axc_ops":
            if key in raw:
                ops = [o.strip().lower() for o in str(raw[key]).split(",") if o.strip()]
                bad = [o for o in ops if o not in KNOWN_OPS]
                if not ops or bad:
                    raise QualityError(f"axc_ops of '{wire}': unknown operator(s) {bad or raw[key]!r} "
                                       f"(known: {', '.join(KNOWN_OPS)})")
                wq.reserved[short] = ops
        else:
            n = _as_int(raw, key, wire, 1)
            if n is not None:
                wq.reserved[short] = n
    return wq


def has_phase2_attrs(raw: Mapping[str, Any]) -> bool:
    """True when ``raw`` carries any attribute beyond the legacy set (drives the cone meta key)."""
    return any(k in raw for k in (QUALITY_ATTRS | set(RESERVED_ATTRS)))


def parse_port_attrs(raw: Mapping[str, Any], port: str) -> Dict[str, Any]:
    """Roles of one input port: {"clock": bool, "reset": None|"high"|"low"}."""
    out: Dict[str, Any] = {"clock": False, "reset": None}
    if "axc_relax" in raw and ("axc_clock" in raw or "axc_reset" in raw):
        raise QualityError(f"'{port}': a clock/reset input cannot also be axc_relax")
    if "axc_clock" in raw:
        v = raw["axc_clock"]
        if v in (0, "0", "false", False, ""):
            pass
        else:
            out["clock"] = True
    if "axc_reset" in raw:
        v = raw["axc_reset"]
        if isinstance(v, str) and v.strip().lower() in ("high", "low"):
            out["reset"] = v.strip().lower()
        elif isinstance(v, int) and not isinstance(v, bool) and v == 1:
            out["reset"] = "high"
        else:
            raise QualityError(f"axc_reset of '{port}': expected \"high\" or \"low\", got {v!r}")
    if out["clock"] and out["reset"]:
        raise QualityError(f"'{port}' cannot be both axc_clock and axc_reset")
    return out
