"""AxBench FIR filter: 8-bit sum of three sinusoids plus Gaussian noise, output SNR."""

import numpy as np

from axfpga.quality import Stimulus, derive_seed, sqnr_db

import importlib.util as _u
import pathlib as _p

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def stimulus(stream, ctx):
    n = np.arange(4000)
    if stream == "tuning":
        ph = (0.0, 0.0, 0.0)
        seed = ctx.seed
    else:  # held-out: derived noise seed and offset sinusoid phases
        seed = derive_seed(ctx.seed, stream)
        r = C.rng_for(ctx, stream)
        ph = tuple(r.uniform(0, 2 * np.pi) for _ in range(3))
    sig = (128 + 50 * np.sin(2 * np.pi * 0.011 * n + ph[0]) + 30 * np.sin(2 * np.pi * 0.047 * n + ph[1])
           + 20 * np.sin(2 * np.pi * 0.19 * n + ph[2]))
    sig = sig + np.random.default_rng(seed).normal(0, 5, n.size)
    x = np.clip(np.round(sig), 0, 255).astype(int)
    port = ctx.inputs[0][0]
    note = "8-bit sum of 3 sinusoids (f=0.011/0.047/0.19 fs) + Gaussian noise (sigma=5)"
    return Stimulus([{port: int(v)} for v in x], note if stream == "tuning" else note + ", held-out phases and noise")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    e = C.col(exact[ctx.skip:n], ctx.relax_wire).astype(np.float64)
    a = C.col(approx[ctx.skip:n], ctx.relax_wire).astype(np.float64)
    return {"name": "Output SNR", "value": sqnr_db(e, a), "unit": "dB", "extra": {"mean_abs_err": float(np.abs(e - a).mean())}}
