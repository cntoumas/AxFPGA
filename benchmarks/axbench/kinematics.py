"""AxBench forward / inverse kinematics (Q15 sign-magnitude): end-effector error (fwd) and joint-angle RMSE (inv)."""

import math

import numpy as np

from axfpga.quality import Stimulus

import importlib.util as _u
import pathlib as _p

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def stimulus(stream, ctx):
    rnd = C.rng_for(ctx, stream)
    if ctx.top == "kinematics":
        vecs = [{"theta1": C.Q15.encode(rnd.uniform(0, math.pi / 2)),
                 "theta2": C.Q15.encode(rnd.uniform(0, math.pi / 2))} for _ in range(2000)]
        return Stimulus(vecs, "joint angles uniform in [0, pi/2] rad, sign-magnitude Q15")
    vecs = []
    for _ in range(2000):
        t1, t2 = rnd.uniform(0.2, math.pi / 2 - 0.2), rnd.uniform(0.2, math.pi / 2 - 0.2)
        x = 11 * math.cos(t1) + 12 * math.cos(t1 + t2)
        y = 11 * math.sin(t1) + 12 * math.sin(t1 + t2)
        vecs.append({"x": C.Q15.encode(x), "y": C.Q15.encode(y)})
    return Stimulus(vecs, "reachable targets from joint angles uniform in [0.2, pi/2-0.2] rad (arm lengths 11, 12), Q15")


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire
    e = np.array([ctx.fmt.decode_real(int(v)) for v in C.col(exact[ctx.skip:n], w)])
    a = np.array([ctx.fmt.decode_real(int(v)) for v in C.col(approx[ctx.skip:n], w)])
    d = np.abs(e - a)
    if ctx.top == "kinematics":
        return {"name": "End-effector |dx| (mean)", "value": float(d.mean()), "unit": "arm units",
                "extra": {"max_abs_dx": float(d.max()), "rmse_dx": float(np.sqrt((d ** 2).mean()))}}
    return {"name": "theta1 RMSE", "value": float(np.sqrt((d ** 2).mean())), "unit": "rad",
            "extra": {"max_abs_dtheta1": float(d.max())}}
