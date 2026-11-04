"""axc_quality grammar: ``metric[opts] op number[%]`` constraints separated by ';', and their evaluation.

Evaluation and failure-message order is fixed (the legacy order of pipeline.assess):
wce, med, mred, er, rmse, the higher-is-better built-ins in text order, then app.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import QualityError
from .metrics import REGISTRY

__all__ = ["QualityError", "Constraint", "QualitySpec"]

_ORDER = ("wce", "med", "mred", "er", "rmse")
_KNOWN = sorted(list(REGISTRY) + ["app"])
_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?:\[([^\]]*)\])?\s*(<=|>=|<|>)\s*"
                 r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(%)?$")


@dataclass(frozen=True)
class Constraint:
    metric: str                       # registry key or "app"
    op: str                           # "<=" "<" ">=" ">"
    threshold: float                  # '%' already divided by 100
    opts: Tuple[Tuple[str, float], ...] = ()
    text: str = ""

    @property
    def higher_is_better(self) -> bool:
        return self.op in (">=", ">")

    def holds(self, v: Optional[float]) -> bool:
        """NaN (or a missing value) counts as a failure."""
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return False
        fail = {"<=": v > self.threshold, "<": v >= self.threshold,
                ">=": v < self.threshold, ">": v <= self.threshold}[self.op]
        return not fail


def _fmt(v: Any, nd: int = 3) -> str:
    if isinstance(v, float):
        return "inf" if math.isinf(v) and v > 0 else ("-inf" if math.isinf(v) else f"{v:.{nd}f}")
    return str(v)


def _is_lower(name: str) -> bool:
    return name in _ORDER


@dataclass(frozen=True)
class QualitySpec:
    constraints: Tuple[Constraint, ...]

    # ------------------------------------------------------------------ construction
    @classmethod
    def parse(cls, text: str, wire: str = "") -> "QualitySpec":
        pre = f"axc_quality of '{wire}': "
        out: List[Constraint] = []
        for raw in str(text).split(";"):
            raw = raw.strip()
            if not raw:
                continue
            m = _RE.match(raw)
            if not m:
                raise QualityError(f"{pre}cannot parse constraint '{raw}'; expected metric op number, e.g. 'wce<=26'")
            name, optstr, op, num, pct = m.group(1).lower(), m.group(2), m.group(3), m.group(4), m.group(5)
            if name != "app" and name not in REGISTRY:
                raise QualityError(f"{pre}unknown metric '{name}' (known: {', '.join(_KNOWN)})")
            if any(c.metric == name for c in out):
                raise QualityError(f"{pre}duplicate constraint on '{name}'")
            if name != "app":
                met = REGISTRY[name]
                if met.higher_is_better and op in ("<=", "<"):
                    raise QualityError(f"{pre}'{raw}' has the wrong direction; {name} is higher-is-better (use >= or >)")
                if not met.higher_is_better and op in (">=", ">"):
                    raise QualityError(f"{pre}'{raw}' has the wrong direction; {name} is lower-is-better (use <= or <)")
            if pct and (name == "app" or not REGISTRY[name].ratio):
                raise QualityError(f"{pre}'%' is not allowed on '{name}'")
            opts: List[Tuple[str, float]] = []
            if optstr is not None and optstr.strip():
                allowed = () if name == "app" else REGISTRY[name].options
                for item in optstr.split(","):
                    k, _, v = item.partition("=")
                    k = k.strip().lower()
                    if k not in allowed:
                        raise QualityError(f"{pre}unknown option '{k}' for '{name}'")
                    try:
                        opts.append((k, float(v)))
                    except ValueError:
                        raise QualityError(f"{pre}cannot parse constraint '{raw}'; expected metric op number, e.g. 'wce<=26'") from None
            thr = float(num) / 100.0 if pct else float(num)
            out.append(Constraint(name, op, thr, tuple(opts), raw))
        if not out:
            raise QualityError(f"{pre}cannot parse constraint ''; expected metric op number, e.g. 'wce<=26'")
        return cls(tuple(out))

    @classmethod
    def legacy(cls, bound: float, metric: str = "WCE", med_limit: Optional[float] = None,
               mred_limit: Optional[float] = None) -> "QualitySpec":
        """Constraints of the pre-Phase-2 DesignSpec fields (budget dict of the suite, or axc_bound alone)."""
        cs: List[Constraint] = []
        prim = "med" if str(metric).upper() == "MED" else "wce"
        cs.append(Constraint(prim, "<=", float(bound), (), f"{prim}<={bound:g}"))
        if med_limit is not None:
            cs.append(Constraint("med", "<=", float(med_limit), (), f"med<={med_limit:g}"))
        if mred_limit is not None:
            cs.append(Constraint("mred", "<=", float(mred_limit), (), f"mred<={mred_limit * 100:g}%"))
        return cls(tuple(cs))

    def with_wce(self, bound: float) -> "QualitySpec":
        """Same constraints with the wce<= threshold replaced (--bound overrides the attribute)."""
        return QualitySpec(tuple(dataclasses.replace(c, threshold=float(bound), text=f"wce<={bound:g}") if c.metric == "wce" else c
                                 for c in self.constraints))

    # ------------------------------------------------------------------ queries
    def get(self, metric: str) -> Optional[Constraint]:
        for c in self.constraints:
            if c.metric == metric:
                return c
        return None

    @property
    def wce_bound(self) -> Optional[float]:
        c = self.get("wce")
        return None if c is None else c.threshold

    @property
    def med_limit(self) -> Optional[float]:
        """The mean-error limit reported in result['limits'] (the last med constraint)."""
        meds = [c for c in self.constraints if c.metric == "med"]
        return meds[-1].threshold if meds else None

    @property
    def mred_limit(self) -> Optional[float]:
        c = self.get("mred")
        return None if c is None else c.threshold

    def ordered(self) -> List[Constraint]:
        rank = {n: i for i, n in enumerate(_ORDER)}
        keyed = []
        for pos, c in enumerate(self.constraints):
            if c.metric in rank:
                keyed.append((rank[c.metric], pos, c))
            elif c.metric == "app":
                keyed.append((len(_ORDER) + 1, pos, c))
            else:
                keyed.append((len(_ORDER), pos, c))
        return [c for _, _, c in sorted(keyed, key=lambda t: (t[0], t[1]))]

    def builtin_higher(self) -> List[Constraint]:
        """Higher-is-better built-in constraints (psnr, sqnr, snr, agreement) in text order."""
        return [c for c in self.constraints if c.metric != "app" and REGISTRY[c.metric].higher_is_better]

    def builtin_metrics(self) -> List[str]:
        return [c.metric for c in self.builtin_higher()]

    # ------------------------------------------------------------------ evaluation
    def apply_app(self, app: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Fill threshold / pass of a plugin metric dict from the ``app`` constraint (in place)."""
        c = self.get("app")
        if app is not None and c is not None:
            app["threshold"] = f"{c.op} {c.threshold!r} {app.get('unit', '')}"
            app["pass"] = bool(c.holds(app.get("value")))
        return app

    def evaluate(self, errs: Mapping[str, Any], extra: Optional[Mapping[str, float]] = None,
                 app: Optional[Dict[str, Any]] = None) -> Tuple[List[str], List[Dict[str, Any]]]:
        """(failure messages in the fixed order, per-constraint records).

        ``errs`` holds wce/med/mred/er (and rmse when constrained); ``extra`` the higher-is-better built-in
        values; ``app`` the plugin metric dict (threshold and pass are filled in).
        """
        vals: Dict[str, Any] = dict(errs)
        vals.update(extra or {})
        self.apply_app(app)
        fails: List[str] = []
        recs: List[Dict[str, Any]] = []
        for c in self.ordered():
            if c.metric == "app":
                v = None if app is None else app.get("value")
            else:
                v = vals.get(c.metric)
            ok = c.holds(v)
            recs.append({"metric": c.metric, "op": c.op, "threshold": c.threshold,
                         "value": None if (isinstance(v, float) and not math.isfinite(v)) else v, "pass": ok})
            if ok:
                continue
            fails.append(self._message(c, v, app))
        if self.get("app") is None and app is not None and app.get("pass") is False:
            # duck-typed evaluator that supplies its own verdict (legacy axbench_apps)
            if not any(r["metric"] in self.builtin_metrics() and not r["pass"] for r in recs):
                fails.append(f"{app['name']} {_fmt(app['value'], 3)} {app['unit']} fails {app['threshold']}")
        return fails, recs

    @staticmethod
    def _message(c: Constraint, v: Any, app: Optional[Mapping[str, Any]]) -> str:
        gt = ">" if c.op == "<=" else ">="
        n = c.metric
        if n == "wce":
            return f"WCE {v:g} {gt} {c.threshold:g}"
        if n == "med":
            return f"MED {v:.3f} {gt} {c.threshold:g}"
        if n == "mred":
            return f"MRED {v * 100:.4f}% {gt} {c.threshold * 100:g}%"
        if n == "er":
            return f"ER {v:.4f} {gt} {c.threshold:g}"
        if n == "rmse":
            return f"RMSE {v:.4g} {gt} {c.threshold:g}"
        if n == "app":
            name, unit = (app or {}).get("name", "app"), (app or {}).get("unit", "")
        else:
            name, unit = REGISTRY[n].label, REGISTRY[n].unit
        return f"{name} {_fmt(v, 3)} {unit} fails {c.op} {c.threshold!r} {unit}"
