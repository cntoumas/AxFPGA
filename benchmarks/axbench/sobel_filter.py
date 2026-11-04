"""AxBench Sobel edge filter: all interior 3x3 windows of a grayscale 128x128 image, edge-image PSNR."""

import numpy as np

from axfpga.quality import Stimulus, psnr_db

import importlib.util as _u
import pathlib as _p

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def stimulus(stream, ctx):
    img = C.gray(stream, 128)
    vecs = []
    for r in range(1, 127):
        for c in range(1, 127):
            win = img[r - 1:r + 2, c - 1:c + 2].flatten()
            vecs.append({f"p{i}": int(win[i]) for i in (0, 1, 2, 3, 5, 6, 7, 8)})
    return Stimulus(vecs, f"{C.IMAGE[stream]} grayscale 128x128, all interior 3x3 windows")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    e = C.col(exact[ctx.skip:n], ctx.relax_wire).astype(np.float64)
    a = C.col(approx[ctx.skip:n], ctx.relax_wire).astype(np.float64)
    return {"name": "Edge-image PSNR", "value": psnr_db(e, a, 255.0), "unit": "dB",
            "extra": {"pixels_differing_pct": float((e != a).mean() * 100)}}
