"""fft64_psd (Phase 4): complex baseband with two tones plus noise, power-spectrum SQNR.

Input: 40 blocks of 64 complex samples (16-bit two's complement, |re|, |im| <= 2^14), in_valid always 1:
a tone of amplitude 6000 on bin 5, a tone of amplitude 3000 between bins (13.37, so it leaks into its neighbours)
and complex Gaussian noise (sigma 200 per component); then 128 zero samples flush the 63-sample delay lines.
Held-out stream: derived noise seed, random tone phases and a different off-bin frequency.
Metric: SQNR of the approximate power spectrum against the exact one. Extra: per-64-sample-window agreement of the
strongest bin (the windows need not be aligned to FFT blocks: exact and approximate are compared position by position).
"""

import importlib.util as _u
import pathlib as _p

import numpy as np

from axfpga.quality import Stimulus, derive_seed, sqnr_db

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).resolve().parent.parent / "axbench" / "_common.py")
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)

N = 64
BLOCKS = 40
FLUSH = 2 * N
LIMIT = 1 << 14


def stimulus(stream, ctx):
    n = np.arange(BLOCKS * N)
    if stream == "tuning":
        ph, f2, seed = (0.0, 0.0), 13.37, ctx.seed
    else:
        r = C.rng_for(ctx, stream)
        ph, f2, seed = (r.uniform(0, 2 * np.pi), r.uniform(0, 2 * np.pi)), 21.61, derive_seed(ctx.seed, stream)
    x = 6000 * np.exp(1j * (2 * np.pi * 5 * n / N + ph[0])) + 3000 * np.exp(1j * (2 * np.pi * f2 * n / N + ph[1]))
    rng = np.random.default_rng(seed)
    x = x + rng.normal(0, 200, n.size) + 1j * rng.normal(0, 200, n.size)
    re = np.clip(np.round(x.real), -LIMIT, LIMIT).astype(int)
    im = np.clip(np.round(x.imag), -LIMIT, LIMIT).astype(int)
    vecs = [{"in_valid": 1, "in_re": int(a), "in_im": int(b)} for a, b in zip(re, im)]
    vecs += [{"in_valid": 1, "in_re": 0, "in_im": 0} for _ in range(FLUSH)]
    note = f"{BLOCKS} x {N} complex samples: tones on bin 5 (6000) and bin {f2} (3000) + noise (sigma=200)"
    return Stimulus(vecs, note if stream == "tuning" else note + ", held-out phases and noise")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = C.col(exact[ctx.skip:n], w).astype(np.float64)
    a = C.col(approx[ctx.skip:n], w).astype(np.float64)
    m = (e.size // N) * N
    agree = float((e[:m].reshape(-1, N).argmax(axis=1) == a[:m].reshape(-1, N).argmax(axis=1)).mean() * 100) if m else 100.0
    return {"name": "Power-spectrum SQNR", "value": sqnr_db(e, a), "unit": "dB",
            "extra": {"peak_bin_agreement_pct": agree, "mean_abs_err": float(np.abs(e - a).mean())}}
