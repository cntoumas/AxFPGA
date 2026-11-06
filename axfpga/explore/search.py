"""Search runner: one exploration per technique (mealpy metaheuristic, exhaustive enumeration, or depth bisection),
the archive of every evaluated point, its Pareto front (cost vs the annotated error metric) and the per-site
explanation of why the chosen sites win."""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from axfpga.explore.techniques import COMP_SET, Technique, genome_key

logger = logging.getLogger("axfpga.explore")

DEFAULT_ALGORITHM = "OriginalTS"     # winner of the algorithm comparison (reports_cluster_explore_20261106_2000/algorithms)
ALGORITHMS = ("EliteSingleGA", "OriginalSA", "OriginalTS", "JADE", "OriginalACOR", "random", "greedy")
BUDGET_FE = 300
BUDGET_S = 900
EARLY_STOP = 30
POP_SIZE = 12
SWEEP_LIMIT = 48
WATCHDOG_GRACE_S = 120


class _Abort(Exception):
    """Raised inside the objective once the watchdog has abandoned a search."""


def _model(name: str, epochs: int):
    from mealpy.evolutionary_based import DE, GA
    from mealpy.math_based import TS
    from mealpy.physics_based import SA
    from mealpy.swarm_based import ACOR
    if name == "EliteSingleGA":
        return GA.EliteSingleGA(epoch=epochs, pop_size=POP_SIZE, pc=0.9, pm=0.1)
    if name == "OriginalSA":
        # step_size and perturbation_scale are absolute: mealpy's defaults (0.1, 0.05) never move an integer gene
        return SA.OriginalSA(epoch=epochs, pop_size=2, step_size=1.0)
    if name == "OriginalTS":
        return TS.OriginalTS(epoch=epochs, neighbour_size=POP_SIZE, perturbation_scale=1.0)
    if name == "JADE":
        return DE.JADE(epoch=epochs, pop_size=POP_SIZE)
    if name == "OriginalACOR":
        return ACOR.OriginalACOR(epoch=epochs, pop_size=POP_SIZE, sample_count=POP_SIZE)
    if name in EVOLUTIONARY:
        import importlib
        mod = importlib.import_module(f"mealpy.evolutionary_based.{EVOLUTIONARY[name][0]}")
        cls = getattr(mod, EVOLUTIONARY[name][1])
        return cls(epoch=epochs, pop_size=POP_SIZE, **EVOLUTIONARY[name][2])
    raise ValueError(f"unknown algorithm {name!r}")


# Further evolutionary / genetic algorithms of mealpy (name -> (module, class, extra parameters)), compared in
# reports_cluster_*/ga_family. Population size POP_SIZE; mealpy defaults otherwise.
EVOLUTIONARY = {
    "BaseGA": ("GA", "BaseGA", {"pc": 0.9, "pm": 0.1}),
    "SingleGA": ("GA", "SingleGA", {"pc": 0.9, "pm": 0.1}),
    "MultiGA": ("GA", "MultiGA", {"pc": 0.9, "pm": 0.1}),
    "EliteMultiGA": ("GA", "EliteMultiGA", {"pc": 0.9, "pm": 0.1}),
    "OriginalMA": ("MA", "OriginalMA", {"max_local_gens": 2}),
    "OriginalES": ("ES", "OriginalES", {}),
    "CMA_ES": ("ES", "CMA_ES", {}),
    "OriginalEP": ("EP", "OriginalEP", {}),
    "OriginalCRO": ("CRO", "OriginalCRO", {}),
    "OriginalSHADE": ("SHADE", "OriginalSHADE", {}),
    "L_SHADE": ("SHADE", "L_SHADE", {}),
    "SADE": ("DE", "SADE", {}),
}


def pareto(points: Sequence[Tuple[float, float, Any]]) -> List[Tuple[float, float, Any]]:
    """Non-dominated (cost, error) points, both minimized, sorted by cost."""
    front: List[Tuple[float, float, Any]] = []
    for c, e, tag in sorted(points, key=lambda p: (p[0], p[1])):
        if not front or e < front[-1][1]:
            front.append((c, e, tag))
    return front


def hypervolume(front: Sequence[Tuple[float, float, Any]], ref_cost: float, ref_err: float) -> float:
    """Area dominated by the front inside [0, ref_cost] x [0, ref_err], normalized to 1."""
    if ref_cost <= 0 or ref_err <= 0:
        return 0.0
    pts = sorted((min(c, ref_cost) / ref_cost, min(e, ref_err) / ref_err) for c, e, _ in front if c < ref_cost and e <= ref_err)
    hv, prev_c = 0.0, 1.0
    for c, e in reversed(pts):        # from the most expensive point down
        hv += (prev_c - c) * (1.0 - e)
        prev_c = c
    return hv


class Exploration:
    """One technique explored with one algorithm against one evaluator."""

    def __init__(self, tech: Technique, evaluator: Any, work_dir: Path, algorithm: str = DEFAULT_ALGORITHM,
                 budget_fe: int = BUDGET_FE, budget_s: float = BUDGET_S, seed: int = 1, jobs: int = 4,
                 primary: str = "wce", sweep: bool = True, warm: Optional[List[Dict[str, Any]]] = None):
        self.tech, self.ev, self.work = tech, evaluator, Path(work_dir)
        self.work.mkdir(parents=True, exist_ok=True)
        self.algorithm, self.budget_fe, self.budget_s, self.seed, self.jobs = algorithm, budget_fe, budget_s, seed, jobs
        self.primary, self.do_sweep, self.warm = primary, sweep, warm or []
        self.archive: List[Dict[str, Any]] = []
        self._memo: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._n = 0
        self.calls = 0
        self.t0 = 0.0
        self.best_trace: List[Tuple[int, float]] = []
        self._abort = False
        self.stalled = False

    # -------------------------------------------------------------- evaluation of one genome
    def evaluate(self, genome: Dict[str, Any], phase: str = "search") -> Dict[str, Any]:
        key = genome_key(genome)
        with self._lock:
            self.calls += 1
            if key in self._memo:
                return self._memo[key]
            self._n += 1
            n = self._n
        name = f"g{n:05d}"
        try:
            paths = self.tech.materialize({name: genome}, self.work / "cand")
            if name not in paths:
                raise RuntimeError("not materialized")
            rec = self.ev.evaluate(paths[name])
        except Exception as exc:
            rec = {"ok": False, "feasible": False, "errors": None, "violation": 1e6, "violations": [str(exc)[:200]],
                   "cost": None, "lcs": None, "luts": None, "dsps": None, "carry4": None, "key": None}
            paths = {}
        row = {"n": n, "phase": phase, "genome": genome, "describe": self.tech.describe(genome),
               "path": str(paths.get(name, "")), "feasible": bool(rec.get("feasible")), "cost": rec.get("cost"),
               "lcs": rec.get("lcs"), "luts": rec.get("luts"), "dsps": rec.get("dsps"), "carry4": rec.get("carry4"),
               "errors": rec.get("errors"), "violation": rec.get("violation"), "violations": rec.get("violations"),
               "fitness": self.ev.fitness(rec), "t": round(time.time() - self.t0, 2)}
        with self._lock:
            self._memo[key] = row
            self.archive.append(row)
            best = min((r["fitness"] for r in self.archive), default=row["fitness"])
            self.best_trace.append((len(self.archive), best))
        return row

    def _sparse(self, rnd: random.Random) -> Dict[str, Any]:
        """A random genome near the exact design: each site active with probability ~2/dims, mostly light depths.
        Approximation spaces are feasible only near the exact design; dense random genomes almost never are."""
        genes = self.tech.genes
        p = min(0.5, 2.0 / max(1, len(genes)))
        ks, cs = [], []
        for g in genes:
            k = 0
            if rnd.random() < p:
                k = rnd.randint(1, max(1, g["max_k"] // 3)) if rnd.random() < 0.7 else rnd.randint(1, g["max_k"])
            ks.append(k)
            if g.get("comp"):
                cs.append(rnd.choice(COMP_SET))
        return self.tech.vector_to_genome(ks, cs)

    def _out_of_budget(self) -> bool:
        return self.calls >= self.budget_fe or (time.time() - self.t0) > self.budget_s

    def _parallel(self, genomes: List[Dict[str, Any]], phase: str) -> List[Dict[str, Any]]:
        out = []
        if self.jobs > 1 and len(genomes) > 1:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                futs = [pool.submit(self.evaluate, g, phase) for g in genomes]
                out = [f.result() for f in futs]
        else:
            out = [self.evaluate(g, phase) for g in genomes]
        return out

    # -------------------------------------------------------------- strategies
    def _exhaustive(self) -> None:
        self._parallel(list(self.tech.all_genomes()), "exhaustive")

    def _bisect(self) -> None:
        """Deepest feasible depth by bisection (error grows with depth), plus its two shallower neighbours."""
        lo, hi = 0, self.tech.size()          # lo feasible (0 = exact), hi + 1 unknown
        while lo < hi and not self._out_of_budget():
            mid = (lo + hi + 1) // 2
            r = self.evaluate({"k": mid}, "bisect")
            if r["feasible"]:
                lo = mid
            else:
                hi = mid - 1
        extra = [{"k": k} for k in (lo - 1, lo - 2) if k >= 1]
        self._parallel(extra, "bisect")

    def _random(self) -> None:
        rnd = random.Random(self.seed)
        while not self._out_of_budget():
            batch = []
            for _ in range(max(1, self.jobs)):
                batch.append(self._sparse(rnd))
            self._parallel(batch, "random")

    def _greedy(self) -> None:
        cur: Dict[str, Any] = {}
        cur_fit = self.ev.base_cost
        while not self._out_of_budget():
            nbrs = []
            for gene in self.tech.genes:
                g0 = cur.get(gene["site"])
                k = (g0["k"] if g0 else 0) + 1
                if k > gene["max_k"]:
                    continue
                for c in (COMP_SET if gene.get("comp") else ("zero",)):
                    if g0 and c != g0.get("comp", "zero"):
                        continue
                    g = {s: dict(v) for s, v in cur.items()}
                    g[gene["site"]] = {"op": gene["op"], "k": k, "comp": c}
                    nbrs.append(g)
            if not nbrs:
                break
            rows = self._parallel(nbrs[: max(1, self.budget_fe - self.calls)], "greedy")
            best = min((r for r in rows if r["feasible"] and r["cost"] is not None), key=lambda r: (r["cost"], r["n"]),
                       default=None)
            if best is None or best["cost"] > cur_fit:
                break
            cur, cur_fit = best["genome"], best["cost"]

    def _mealpy(self) -> None:
        from mealpy import CategoricalVar, IntegerVar, Problem

        tech, ev = self.tech, self
        ub = [g["max_k"] for g in tech.genes]
        n_comp = sum(1 for g in tech.genes if g.get("comp"))
        bounds = [IntegerVar(lb=[0] * len(ub), ub=ub, name="k")]
        if n_comp:
            bounds.append(CategoricalVar(valid_sets=[COMP_SET] * n_comp, name="c"))

        class P(Problem):
            def __init__(self):
                super().__init__(bounds, minmax="min", log_to=None)

            def obj_func(self, x):
                if ev._abort or (time.time() - ev.t0) > ev.budget_s + WATCHDOG_GRACE_S:
                    raise _Abort()
                d = self.decode_solution(x)
                g = tech.vector_to_genome([int(v) for v in np.atleast_1d(d["k"])], list(d["c"]) if n_comp else None)
                return ev.evaluate(g, "search")["fitness"]

        prob = P()
        epochs = max(2, self.budget_fe)
        model = _model(self.algorithm, epochs)
        term = {"max_fe": self.budget_fe, "max_time": self.budget_s, "max_early_stop": EARLY_STOP}
        def encode(g: Dict[str, Any]) -> np.ndarray:
            ks = [int(g.get(gene["site"], {}).get("k", 0)) for gene in tech.genes]
            cs = [g.get(gene["site"], {}).get("comp", "zero") for gene in tech.genes if gene.get("comp")]
            vec = list(prob.bounds[0].encode(ks))
            if n_comp:
                vec += list(prob.bounds[1].encode(cs))
            return np.array(vec, dtype=float)

        starts = None
        pop = getattr(model, "pop_size", None)
        if pop:
            rnd = random.Random(self.seed)
            seeds = list(self.warm[: max(1, pop // 2)]) + [{}]
            while len(seeds) < pop:
                seeds.append(self._sparse(rnd))
            starts = [encode(g) for g in seeds[:pop]]
        # Watchdog: mealpy's own termination is checked per epoch, and some algorithms can loop inside an epoch
        # without evaluating anything (seen with EliteSingleGA once every genome is memoized). The solve runs in a
        # daemon thread; past the time budget (+ grace) the search is abandoned and the archive so far is used.
        err: List[BaseException] = []

        def solve() -> None:
            try:
                try:
                    model.solve(prob, mode="thread" if self.jobs > 1 else "single", n_workers=max(1, self.jobs),
                                termination=term, starting_solutions=starts, seed=self.seed)
                except _Abort:
                    raise
                except Exception as exc:
                    logger.warning("%s in thread mode failed (%s); running single", self.algorithm, exc)
                    model.solve(prob, mode="single", termination=term, starting_solutions=starts, seed=self.seed)
            except _Abort:
                pass
            except BaseException as exc:      # reported, never raised into the pipeline
                err.append(exc)

        th = threading.Thread(target=solve, daemon=True)
        th.start()
        th.join(self.budget_s + WATCHDOG_GRACE_S)
        if th.is_alive():
            self._abort = True
            self.stalled = True
            logger.warning("%s exceeded its time budget by %ds without returning; search abandoned (archive kept)",
                           self.algorithm, WATCHDOG_GRACE_S)
        elif err:
            logger.warning("%s failed: %s", self.algorithm, err[0])
        self.history = list(getattr(getattr(model, "history", None), "list_global_best_fit", []) or [])

    # -------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        self.t0 = time.time()
        tech = self.tech
        size = tech.size()
        if tech.method == "bisect":
            strategy = "bisection"
            self._bisect()
        elif size <= self.budget_fe or tech.method == "enumerate":
            strategy = "exhaustive"
            self._exhaustive()
        else:
            if self.do_sweep:
                sweep = tech.single_site_genomes()[:SWEEP_LIMIT]
                self._parallel(sweep, "sweep")
            strategy = self.algorithm
            if self.algorithm == "random":
                self._random()
            elif self.algorithm == "greedy":
                self._greedy()
            else:
                if not self.warm:
                    feas = sorted((r for r in self.archive if r["feasible"] and r["cost"] is not None), key=lambda r: r["cost"])
                    self.warm = [r["genome"] for r in feas[:POP_SIZE // 2]]
                self._mealpy()
        elapsed = time.time() - self.t0
        return self.summary(strategy, size, elapsed)

    # -------------------------------------------------------------- results
    def feasible(self) -> List[Dict[str, Any]]:
        return sorted((r for r in self.archive if r["feasible"] and r["cost"] is not None),
                      key=lambda r: (r["cost"], r["errors"]["wce"] if r["errors"] else 0, r["n"]))

    def front(self) -> List[Dict[str, Any]]:
        pts = [(r["cost"], r["errors"].get(self.primary, 0.0), r) for r in self.archive
               if r["cost"] is not None and r["errors"]]
        return [r for _, _, r in pareto(pts)]

    def explanation(self) -> Dict[str, Any]:
        """Per-site table: cost saved and error added per unit of k (one-site sweep), use among the best genomes."""
        base = self.ev.base_cost
        limit = self.ev.limits.get(self.primary)
        sweep = [r for r in self.archive if r["phase"] in ("sweep", "exhaustive", "greedy") and len(r["genome"]) == 1
                 and isinstance(next(iter(r["genome"].values())), dict)]
        feas = self.feasible()
        top = feas[: max(1, len(feas) // 10)] if feas else []
        best = feas[0] if feas else None
        rows = []
        for gene in self.tech.genes:
            sid = gene["site"]
            mine = [r for r in sweep if sid in r["genome"]]
            per = []
            for r in mine:
                k = r["genome"][sid]["k"]
                err = (r["errors"] or {}).get(self.primary)
                per.append({"k": k, "feasible": r["feasible"], "cost": r["cost"],
                            "saved": None if r["cost"] is None else base - r["cost"], "error": err})
            ok = [p for p in per if p["feasible"] and p["saved"] is not None]
            eff = None
            if ok:
                p = max(ok, key=lambda p: (p["saved"], -p["k"]))
                frac = (p["error"] / limit) if (limit and p["error"] is not None) else None
                # LC saved per percent of the error budget used (floor 0.1 %: error-free rewrites stay finite)
                eff = p["saved"] / max(100.0 * frac, 0.1) if frac is not None else p["saved"]
            used = [r["genome"][sid]["k"] for r in top if sid in r["genome"]]
            rows.append({"site": sid, "op": gene["op"], "kind": gene.get("kind"), "max_k": gene["max_k"],
                         "sweep": per,
                         "lc_saved_per_k": (max(p["saved"] / p["k"] for p in ok) if ok else None),
                         "error_per_k": (min(p["error"] / p["k"] for p in per if p["error"] is not None) if any(p["error"] is not None for p in per) else None),
                         "efficiency": eff,
                         "in_top_pct": round(100.0 * len(used) / len(top), 1) if top else 0.0,
                         "mean_k_in_top": round(sum(used) / len(used), 2) if used else 0.0,
                         "k_in_best": (best["genome"].get(sid, {}).get("k", 0) if best and isinstance(best["genome"].get(sid), dict) else 0)})
        rows.sort(key=lambda r: (0 if r["k_in_best"] else 1, -(r["efficiency"] or -1e9), r["site"]))
        why = []
        if best:
            chosen = [r for r in rows if r["k_in_best"]]
            for r in chosen[:6]:
                why.append(f"{r['site']} (k={r['k_in_best']}): saves {r['lc_saved_per_k'] or 0:.1f} LC per bit alone, "
                           f"adds {r['error_per_k'] or 0:.3g} {self.primary.upper()} per bit; in {r['in_top_pct']}% of the best 10% genomes")
        return {"primary": self.primary, "limit": limit, "base_cost": base, "sites": rows, "why": why}

    def summary(self, strategy: str, size: int, elapsed: float) -> Dict[str, Any]:
        feas = self.feasible()
        best = feas[0] if feas else None
        front = self.front()
        limit = self.ev.limits.get(self.primary)
        hv = hypervolume([(r["cost"], r["errors"].get(self.primary, 0.0), None) for r in front], self.ev.base_cost,
                         limit or max([r["errors"].get(self.primary, 0.0) for r in front] + [1.0]))
        conv = []
        b = math.inf
        for r in sorted(self.archive, key=lambda r: r["n"]):
            if r["feasible"] and r["cost"] is not None:
                b = min(b, r["cost"])
            conv.append(None if b == math.inf else b)
        arch_path = self.work / "archive.jsonl"
        with open(arch_path, "w") as f:
            for r in self.archive:
                f.write(json.dumps(r) + "\n")

        def slim(r):
            if r is None:
                return None
            return {k: r[k] for k in ("n", "genome", "describe", "path", "cost", "lcs", "luts", "dsps", "carry4", "errors", "feasible")}

        evals_to_95 = None
        if best and best["cost"] < self.ev.base_cost:
            target = self.ev.base_cost - 0.95 * (self.ev.base_cost - best["cost"])
            evals_to_95 = next((i + 1 for i, c in enumerate(conv) if c is not None and c <= target), None)
        return {
            "technique": self.tech.name, "description": self.tech.description, "level": self.tech.level,
            "strategy": strategy, "algorithm": self.algorithm if strategy == self.algorithm else strategy,
            "seed": self.seed, "stalled": self.stalled, "space_size": size if size < 10 ** 15 else float(size), "dims": self.tech.dims(),
            "evaluations": len(self.archive), "calls": self.calls, "time_s": round(elapsed, 1),
            "base_cost": self.ev.base_cost, "best": slim(best),
            "feasible_points": len(feas), "front": [slim(r) for r in front], "hypervolume": round(hv, 5),
            "evals_to_95pct": evals_to_95, "convergence": conv, "archive": str(arch_path),
            "explanation": self.explanation() if self.tech.genes else None,
            "ranked": [slim(r) for r in feas[:8]],
        }
