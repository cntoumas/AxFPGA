"""AxBench neural-network perceptron: random synapses and weights in [-1,1] (Q9 sign-magnitude), decision agreement."""

import numpy as np

from axfpga.quality import Stimulus, sqnr_db

import importlib.util as _u
import pathlib as _p

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def stimulus(stream, ctx):
    rnd = C.rng_for(ctx, stream)
    vecs = []
    for _ in range(3000):
        syn = [C.Q9.encode(rnd.uniform(-1, 1)) for _ in range(8)]
        wts = [C.Q9.encode(rnd.uniform(-1, 1)) for _ in range(8)]
        vecs.append({"synapse_in": C.pack(syn, 16), "weight_in": C.pack(wts, 16)})
    return Stimulus(vecs, "seeded random inputs and weights in [-1,1] (Q9); no dataset in AxBench")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = np.array([ctx.fmt.decode_real(int(v)) for v in C.col(exact[ctx.skip:n], w)])
    a = np.array([ctx.fmt.decode_real(int(v)) for v in C.col(approx[ctx.skip:n], w)])
    thr = 0.5 if (e.min() >= 0 and e.max() <= 1.0) else 0.0
    agree = float(((e >= thr) == (a >= thr)).mean() * 100)
    return {"name": "Decision agreement", "value": agree, "unit": "%",
            "extra": {"SQNR_dB": sqnr_db(e, a), "decision_threshold": thr}}
