"""AxBench adders and multipliers: uniform random operands, SQNR of the result (exact vs approximate hardware)."""

import numpy as np

from axfpga.quality import Stimulus, sqnr_db

import importlib.util as _u
import pathlib as _p

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def stimulus(stream, ctx):
    rnd = C.rng_for(ctx, stream)
    # name order, not Yosys port order: legacy stream draws IN1 (X) first, then IN2 (Y)
    (a, w), (b, _) = sorted(ctx.inputs[:2])
    vecs = [{a: rnd.getrandbits(w), b: rnd.getrandbits(w)} for _ in range(3000)]
    return Stimulus(vecs, "uniform random operands")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    dec = ctx.fmt.decode if ctx.fmt is not None else int
    e = np.array([dec(int(r[ctx.relax_wire])) for r in exact[ctx.skip:n]], dtype=np.float64)
    a = np.array([dec(int(r[ctx.relax_wire])) for r in approx[ctx.skip:n]], dtype=np.float64)
    err = np.abs(e - a)
    nz = e != 0
    sq = sqnr_db(e, a)
    return {"name": "SQNR", "value": sq, "unit": "dB",
            "extra": {"NMED": float(err.mean() / (2 ** ctx.width - 1)),
                      "MRED_pct": float((err[nz] / np.abs(e[nz])).mean() * 100) if nz.any() else 0.0}}
