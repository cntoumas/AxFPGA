"""conv2d_3x3 (Phase 4): 3x3 RGB windows of a 34x34 image through one convolution kernel, feature-map PSNR.

Kernel: the 3x3 emboss filter ((-2,-1,0), (-1,1,1), (0,1,2)) applied to luma, split over the R, G, B channels with
the BT.601 weights (0.299, 0.587, 0.114) and scaled by 2^6 (the RTL SHIFT). The kernel sums to 1, so the output is
the luma plus a directional gradient and uses the whole 0..255 range; ReLU clips only dark edges. It stands for a
trained first-layer filter (oriented edge detector); AxBench has no trained CNN weights.
Tuning stream: peppers.png; held-out stream: baboon.gaussian.jpg (both from the AxBench checkout).
"""

import importlib.util as _u
import pathlib as _p

import numpy as np

from axfpga.quality import Stimulus, psnr_db, sqnr_db

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).resolve().parent.parent / "axbench" / "_common.py")
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)

SIZE = 34                                   # image side; 32 x 32 = 1024 output pixels
SHIFT = 6                                   # must match conv2d_3x3 #(.SHIFT)
LUMA = (0.299, 0.587, 0.114)
EMBOSS = ((-2, -1, 0), (-1, 1, 1), (0, 1, 2))
WEIGHTS = [int(round(LUMA[c] * EMBOSS[r][k] * (1 << SHIFT))) for c in range(3) for r in range(3) for k in range(3)]


def stimulus(stream, ctx):
    img = C.rgb(stream, SIZE)
    w = C.pack(WEIGHTS, 8)
    vecs = []
    for r in range(1, SIZE - 1):
        for c in range(1, SIZE - 1):
            win = [int(v) for ch in range(3) for v in img[r - 1:r + 2, c - 1:c + 2, ch].flatten()]
            vecs.append({"in_valid": 1, "win": C.pack(win, 8), "weights": w})
    return Stimulus(vecs, f"{C.IMAGE[stream]} {SIZE}x{SIZE} RGB, 3x3 windows; luma emboss kernel split over R/G/B "
                          f"(weights x 2^{SHIFT}, no trained weights in AxBench)")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = C.col(exact[ctx.skip:n], w).astype(np.float64)
    a = C.col(approx[ctx.skip:n], w).astype(np.float64)
    return {"name": "Feature-map PSNR", "value": psnr_db(e, a, 255.0), "unit": "dB",
            "extra": {"SQNR_dB": sqnr_db(e, a), "mean_abs_err": float(np.abs(e - a).mean())}}
