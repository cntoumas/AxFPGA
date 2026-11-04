"""AxBench Sobel NPU element: 3x3 image windows through one perceptron with a seeded weight set, output-image PSNR."""

import numpy as np

from axfpga.quality import Stimulus, psnr_db, sqnr_db

import importlib.util as _u
import pathlib as _p

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def stimulus(stream, ctx):
    rnd = C.rng_for(ctx, stream)
    img = C.gray(stream, 32)
    hidden_w = [C.Q9.encode(rnd.uniform(-1, 1)) for _ in range(9)]
    weight_in = [C.Q9.encode(rnd.uniform(-1, 1)) for _ in range(80)]
    hw, wi = C.pack(hidden_w, 16), C.pack(weight_in, 16)
    vecs = []
    for r in range(1, 31):
        for c in range(1, 31):
            win = img[r - 1:r + 2, c - 1:c + 2].flatten()
            syn = [C.Q9.encode(p / 255.0) for p in win] + [C.Q9.encode(1.0)]
            vecs.append({"synapse_in": C.pack(syn, 16), "weight_in": wi, "hidden_weight": hw})
    return Stimulus(vecs, f"{C.IMAGE[stream]} 32x32 3x3 windows (Q9, bias=1.0); one fixed seeded random weight set in [-1,1] "
                          "(no trained weights in AxBench)")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = np.array([ctx.fmt.decode_real(int(v)) for v in C.col(exact[ctx.skip:n], w)])
    a = np.array([ctx.fmt.decode_real(int(v)) for v in C.col(approx[ctx.skip:n], w)])
    peak = float(e.max() - e.min()) or 1.0
    return {"name": "Output-image PSNR", "value": psnr_db(e, a, peak), "unit": "dB",
            "extra": {"SQNR_dB": sqnr_db(e, a), "peak": peak}}
