"""Stimulus sources: seeded random, hex file, tuning / held-out stream split, power-simulation cap."""

from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .errors import QualityError

SEED_TUNING = "tuning"
SEED_HELDOUT = "heldout"
STREAMS = (SEED_TUNING, SEED_HELDOUT)
MIN_FILE_VECTORS = 128
POWER_VECTORS = 2000

_ON = re.compile(r"(^|_)(en|enable|valid|start|ce|run|load)($|_)", re.I)
_OFF = re.compile(r"(^|_)(clear|clr|flush|zero|reset|rst)($|_)", re.I)


def derive_seed(seed: int, stream: str) -> int:
    """Tuning keeps the seed unchanged (the legacy stream); held-out is a hash-derived independent seed."""
    if stream == SEED_TUNING:
        return int(seed)
    if stream == SEED_HELDOUT:
        return int.from_bytes(hashlib.sha256(f"{seed}:{stream}".encode()).digest()[:8], "big") & 0x7FFFFFFF
    raise QualityError(f"unknown stream '{stream}' (expected 'tuning' or 'heldout')")


def random_vectors(inputs: Sequence[Tuple[str, int]], n: int, seed: int, stream: str = SEED_TUNING) -> Tuple[List[Dict[str, int]], Dict[str, Any]]:
    """Moved from GenericEvaluator.build_stimulus: enable-like 1-bit inputs mostly 1, clear-like mostly 0."""
    s = derive_seed(seed, stream)
    rng = random.Random(s)
    vecs: List[Dict[str, int]] = []
    for _ in range(n):
        v = {}
        for name, w in inputs:
            if w == 1 and _ON.search(name):
                v[name] = int(rng.random() < 0.9)
            elif w == 1 and _OFF.search(name):
                v[name] = int(rng.random() < 0.05)
            else:
                v[name] = rng.getrandbits(w)
        vecs.append(v)
    note = f"seeded random stimulus ({n} vectors, seed {s})"
    if stream == SEED_HELDOUT:
        note += " held-out"
    return vecs, {"note": note}


def resolve_path(p: str, base_dir: Path, search_dirs: Sequence[Path] = ()) -> Path:
    """Absolute path as is; relative path against base_dir, then search_dirs. QualityError listing the places tried."""
    q = Path(p)
    tried: List[Path] = []
    for cand in ([q] if q.is_absolute() else [Path(base_dir) / q, *[Path(d) / q for d in search_dirs]]):
        if cand.is_file():
            return cand
        tried.append(cand.parent)
    raise QualityError(f"file '{p}' not found (looked in: {', '.join(str(t) for t in tried)})")


def load_hex(path, inputs: Sequence[Tuple[str, int]]) -> List[Dict[str, int]]:
    """One line per cycle, one hex word per input in port order. Blank lines and '#' / '//' comments are skipped."""
    vecs: List[Dict[str, int]] = []
    for ln, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = re.split(r"#|//", line, maxsplit=1)[0].strip()
        if not line:
            continue
        words = line.split()
        if len(words) != len(inputs):
            raise QualityError(f"line {ln} has {len(words)} words, the design has {len(inputs)} data inputs")
        row = {}
        for (name, w), tok in zip(inputs, words):
            try:
                val = int(tok, 16)
            except ValueError:
                raise QualityError(f"line {ln}: '{tok}' is not a hex word") from None
            row[name] = val & ((1 << w) - 1)
        vecs.append(row)
    return vecs


def split_file_vectors(vecs: Sequence[Dict[str, int]]) -> Tuple[List[Dict[str, int]], List[Dict[str, int]]]:
    """Contiguous halves: first = tuning, second = held-out."""
    if len(vecs) < MIN_FILE_VECTORS:
        raise QualityError(f"file has {len(vecs)} vectors; at least {MIN_FILE_VECTORS} are needed to split into tuning and held-out streams")
    h = (len(vecs) + 1) // 2
    return list(vecs[:h]), list(vecs[h:])


def power_cap(vectors: Sequence[Dict[str, int]], n: int = POWER_VECTORS) -> List[Dict[str, int]]:
    """The application stream capped at n evenly spaced cycles (axbench_apps.power_vectors)."""
    if len(vectors) <= n:
        return list(vectors)
    step = len(vectors) / n
    return [vectors[int(i * step)] for i in range(n)]
