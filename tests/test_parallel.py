import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from axfpga.parallel import depth_search, first_accepted, yosys_serialized


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("workers,speculate", [(2, 1), (3, 2), (4, 2)])
def test_depth_search_matches_serial(seed, workers, speculate):
    rnd = random.Random(seed)
    k_max = rnd.randint(1, 60)
    max_evals = rnd.randint(1, 8)
    ok = {k: rnd.random() < 0.5 for k in range(0, k_max + 1)}  # non-monotone on purpose
    calls = []

    def evaluate(k):
        calls.append(k)
        time.sleep(rnd.random() * 0.002)
        return ("res", k)

    def is_ok(r):
        return ok[r[1]]

    serial_visited, serial_lo = depth_search(k_max, max_evals, evaluate, is_ok)
    with ThreadPoolExecutor(workers) as pool:
        par_visited, par_lo = depth_search(k_max, max_evals, evaluate, is_ok, executor=pool, speculate=speculate)
    assert par_visited == serial_visited
    assert par_lo == serial_lo
    assert len(serial_visited) <= max_evals


def test_depth_search_speculation_reuses_results():
    calls = []

    def evaluate(k):
        calls.append(k)
        return k

    with ThreadPoolExecutor(3) as pool:
        visited, _ = depth_search(40, 7, evaluate, lambda r: r <= 17, executor=pool, speculate=2)
    # each depth is evaluated at most once even though it may be both speculated and visited
    assert len(calls) == len(set(calls))
    assert {k for k, _ in visited} <= set(calls)


@pytest.mark.parametrize("seed", range(30))
def test_first_accepted_matches_serial(seed):
    rnd = random.Random(seed)
    items = list(range(rnd.randint(0, 12)))
    good = {i: rnd.random() < 0.25 for i in items}
    cancel = threading.Event()

    def run(i):
        time.sleep(rnd.random() * 0.002)
        return i

    serial = first_accepted(items, run, lambda r: good[r])
    with ThreadPoolExecutor(3) as pool:
        par = first_accepted(items, run, lambda r: good[r], executor=pool, cancel=cancel)
    assert par == serial
    assert cancel.is_set()


def test_first_accepted_propagates_error_of_consumed_item_only():
    def run(i):
        if i == 1:
            raise RuntimeError("boom")
        return i

    with ThreadPoolExecutor(2) as pool:
        with pytest.raises(RuntimeError):
            first_accepted([0, 1, 2], run, lambda r: False, executor=pool)
        # item 1 comes after the accepted item 0: its error is never consumed
        consumed, idx = first_accepted([0, 1, 2], run, lambda r: r == 0, executor=pool)
    assert idx == 0 and consumed == [(0, 0)]


def test_yosys_serialized_never_overlaps():
    active = 0
    peak = 0
    guard = threading.Lock()

    @yosys_serialized
    def critical():
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with guard:
            active -= 1

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda _: critical(), range(12)))
    assert peak == 1


def test_yosys_serialized_is_reentrant():
    @yosys_serialized
    def inner():
        return 1

    @yosys_serialized
    def outer():
        return inner() + 1

    assert outer() == 2
