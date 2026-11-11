"""Parallel evaluation helpers."""

from __future__ import annotations

import functools
import threading
from concurrent.futures import Executor, Future
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

YOSYS_LOCK = threading.RLock()

F = TypeVar("F", bound=Callable[..., Any])


def yosys_serialized(fn: F) -> F:
    """Run ``fn`` while holding the process-wide pyosys lock."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with YOSYS_LOCK:
            return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


class Cancelled(Exception):
    """Raised inside a worker when its speculative evaluation is no longer needed."""


def _next_mid(lo: int, hi: int, n: int, max_evals: int) -> Optional[int]:
    if lo < hi and n < max_evals:
        return (lo + hi + 1) // 2
    return None


def depth_search(
    k_max: int,
    max_evals: int,
    evaluate: Callable[[int], Any],
    is_ok: Callable[[Any], bool],
    executor: Optional[Executor] = None,
    speculate: int = 2,
) -> Tuple[List[Tuple[int, Any]], int]:
    """Binary search for the largest depth in [0, k_max] whose result satisfies ``is_ok``."""
    futures: Dict[int, Future] = {}

    def submit(k: int) -> Future:
        if k not in futures:
            futures[k] = executor.submit(evaluate, k)  # type: ignore[union-attr]
        return futures[k]

    visited: List[Tuple[int, Any]] = []
    lo, hi, n = 0, k_max, 0
    while True:
        mid = _next_mid(lo, hi, n, max_evals)
        if mid is None:
            break
        n += 1
        if executor is None:
            result = evaluate(mid)
        else:
            fut = submit(mid)
            if speculate > 0 and n < max_evals:
                if mid < hi:
                    submit((mid + hi + 1) // 2)              # next mid if mid passes
                if speculate > 1 and lo < mid - 1:
                    submit((lo + mid) // 2)                  # next mid if mid fails
            result = fut.result()
        visited.append((mid, result))
        if is_ok(result):
            lo = mid
        else:
            hi = mid - 1
        if executor is not None:
            keep = {k for k, _ in visited}
            nxt = _next_mid(lo, hi, n, max_evals)
            if nxt is not None:
                keep.add(nxt)
            for k, f in futures.items():
                if k not in keep:
                    f.cancel()
    return visited, lo


def first_accepted(
    items: Sequence[Any],
    run: Callable[[Any], Any],
    accept: Callable[[Any], bool],
    executor: Optional[Executor] = None,
    cancel: Optional[threading.Event] = None,
) -> Tuple[List[Tuple[Any, Any]], Optional[int]]:
    """Run ``run(item)`` over ``items`` and find the first (in order) whose result is accepted."""
    futures: List[Future] = []
    if executor is not None:
        futures = [executor.submit(run, it) for it in items]
    consumed: List[Tuple[Any, Any]] = []
    found: Optional[int] = None
    try:
        for i, it in enumerate(items):
            result = futures[i].result() if executor is not None else run(it)
            consumed.append((it, result))
            if accept(result):
                found = i
                break
    finally:
        if cancel is not None:
            cancel.set()
        for f in futures:
            f.cancel()
    return consumed, found
