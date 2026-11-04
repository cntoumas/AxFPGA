"""Helpers shared by the AxBench plugins (plain functions; plugins import this by file path, see load below)."""

from __future__ import annotations

import random
from typing import List

import numpy as np
from PIL import Image

from axfpga.config import axbench_root
from axfpga.quality import NumberFormat, derive_seed

IMAGES = axbench_root() / "scripts" / "images"      # AXFPGA_AXBENCH_ROOT overrides
IMAGE = {"tuning": "peppers.png", "heldout": "baboon.gaussian.jpg"}
Q9 = NumberFormat("sm", 16, 9)
Q15 = NumberFormat("sm", 32, 15)


def rng_for(ctx, stream: str) -> random.Random:
    """Tuning: Random(seed), the legacy stream. Held-out: Random(derived seed)."""
    return random.Random(derive_seed(ctx.seed, stream))


def image_path(stream: str):
    p = IMAGES / IMAGE[stream]
    if not p.is_file():
        raise FileNotFoundError(f"image {p} not found; set AXFPGA_AXBENCH_ROOT to the AxBench checkout")
    return p


def gray(stream: str, size: int) -> np.ndarray:
    return np.asarray(Image.open(image_path(stream)).convert("L").resize((size, size), Image.BILINEAR), dtype=np.int64)


def rgb(stream: str, size: int) -> np.ndarray:
    return np.asarray(Image.open(image_path(stream)).convert("RGB").resize((size, size), Image.BILINEAR), dtype=np.int64)


def pack(vals: List[int], w: int) -> int:
    out = 0
    for i, v in enumerate(vals):
        out |= (v & ((1 << w) - 1)) << (i * w)
    return out


def col(rows, name):
    return np.array([r[name] for r in rows], dtype=object)
