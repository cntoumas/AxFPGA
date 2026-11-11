"""image_pipeline (Phase 4): a 64x64 RGB image streamed in raster order, edge-image PSNR."""

import importlib.util as _u
import pathlib as _p

import numpy as np

from axfpga.quality import Stimulus, psnr_db, sqnr_db

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).resolve().parent.parent / "axbench" / "_common.py")
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)

WIDTH = 64                                  # must match image_pipeline #(.WIDTH)
FLUSH = 2 * (2 * WIDTH + 2) + 8


def stimulus(stream, ctx):
    img = C.rgb(stream, WIDTH)
    vecs = [{"in_valid": 1, "rgb_in": (int(p[0]) << 16) | (int(p[1]) << 8) | int(p[2])}
            for row in img for p in row]
    vecs += [{"in_valid": 1, "rgb_in": 0} for _ in range(FLUSH)]
    return Stimulus(vecs, f"{C.IMAGE[stream]} {WIDTH}x{WIDTH} RGB, raster order, {FLUSH} flush pixels")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = C.col(exact[ctx.skip:n], w).astype(np.float64)
    a = C.col(approx[ctx.skip:n], w).astype(np.float64)
    return {"name": "Edge-image PSNR", "value": psnr_db(e, a, 255.0), "unit": "dB",
            "extra": {"SQNR_dB": sqnr_db(e, a), "mean_abs_err": float(np.abs(e - a).mean())}}
