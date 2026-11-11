"""iir_biquad (Phase 4): low-pass biquad on a passband tone plus a stopband tone plus noise, output SNR."""

import importlib.util as _u
import pathlib as _p

import numpy as np

from axfpga.quality import Stimulus, derive_seed, sqnr_db

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).resolve().parent.parent / "axbench" / "_common.py")
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)

SAMPLES = 4000
LIMIT = 1 << 14


def stimulus(stream, ctx):
    n = np.arange(SAMPLES)
    if stream == "tuning":
        ph = (0.0, 0.0)
        seed = ctx.seed
    else:
        seed = derive_seed(ctx.seed, stream)
        r = C.rng_for(ctx, stream)
        ph = tuple(r.uniform(0, 2 * np.pi) for _ in range(2))
    sig = 6000 * np.sin(2 * np.pi * 0.01 * n + ph[0]) + 3000 * np.sin(2 * np.pi * 0.2 * n + ph[1])
    sig = sig + np.random.default_rng(seed).normal(0, 300, n.size)
    x = np.clip(np.round(sig), -LIMIT, LIMIT).astype(int)
    note = "16-bit tones at 0.01 fs (6000) and 0.2 fs (3000) + Gaussian noise (sigma=300)"
    return Stimulus([{"in_valid": 1, "x_in": int(v)} for v in x],
                    note if stream == "tuning" else note + ", held-out phases and noise")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = np.array([ctx.fmt.decode(int(v)) for v in C.col(exact[ctx.skip:n], w)], dtype=np.float64)
    a = np.array([ctx.fmt.decode(int(v)) for v in C.col(approx[ctx.skip:n], w)], dtype=np.float64)
    return {"name": "Output SNR", "value": sqnr_db(e, a), "unit": "dB",
            "extra": {"mean_abs_err": float(np.abs(e - a).mean()), "max_abs_err": float(np.abs(e - a).max())}}
