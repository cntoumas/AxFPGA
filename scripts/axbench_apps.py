"""Application-level stimulus and metrics for the 13 AxBench circuits.

Every benchmark gets a deterministic (seeded) per-cycle input stream for its top-level
ports. The exact and approximated tops are simulated with the same stream, and the
application metric compares their output streams. Errors are always measured against
the exact hardware, not against floating-point ground truth.

AxBench ships no application datasets (no NN weights/labels, no k-means engine around
sub_8b); the only shared input is scripts/images/peppers.png. Choices made here are
labelled in each metric's "note".
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from axfpga.config import axbench_root
from PIL import Image

PEPPERS = axbench_root() / "scripts" / "images" / "peppers.png"   # AXFPGA_AXBENCH_ROOT overrides
SEED = 2026
POWER_VECTORS = 2000

ARITH = {"brent_kung_16b", "brent_kung_32b", "kogge_stone_16b", "kogge_stone_32b", "multiplier_16b", "multiplier_32b"}


# --------------------------------------------------------------------------- fixed point
def sm_encode(x: float, frac: int, width: int) -> int:
    """Sign-magnitude fixed point (MSB = sign), as used by AxBench qadd/qmult."""
    mag = min(int(round(abs(x) * (1 << frac))), (1 << (width - 1)) - 1)
    return ((1 << (width - 1)) | mag) if x < 0 and mag else mag


def sm_decode(v: int, frac: int, width: int) -> float:
    mag = v & ((1 << (width - 1)) - 1)
    return (-mag if (v >> (width - 1)) & 1 else mag) / float(1 << frac)


def _pack(vals: List[int], w: int) -> int:
    out = 0
    for i, v in enumerate(vals):
        out |= (v & ((1 << w) - 1)) << (i * w)
    return out


def _gray(size: int) -> np.ndarray:
    return np.asarray(Image.open(PEPPERS).convert("L").resize((size, size), Image.BILINEAR), dtype=np.int64)


def _rgb(size: int) -> np.ndarray:
    return np.asarray(Image.open(PEPPERS).convert("RGB").resize((size, size), Image.BILINEAR), dtype=np.int64)


# --------------------------------------------------------------------------- output number formats
# How the relaxed output word is read: ("unsigned", W) | ("twos", W) | ("sm", W) sign-magnitude, in LSB units.
OUTPUT_FORMAT: Dict[str, Tuple[str, int]] = {
    "multiplier_16b": ("twos", 32),
    "multiplier_32b": ("twos", 64),
    "kmeans_clustering": ("twos", 9),
    "kinematics_fwd": ("sm", 32),
    "kinematics_inv": ("sm", 32),
    "neural_network": ("sm", 16),
    "sobel_npu": ("sm", 16),
}


def decode(bench_id: str, value: int) -> int:
    kind, w = OUTPUT_FORMAT.get(bench_id, ("unsigned", 0))
    if kind == "twos":
        value &= (1 << w) - 1
        return value - (1 << w) if value >> (w - 1) else value
    if kind == "sm":
        mag = value & ((1 << (w - 1)) - 1)
        return -mag if (value >> (w - 1)) & 1 else mag
    return value


def error_stats(bench_id: str, exact: List[Dict[str, int]], approx: List[Dict[str, int]], wire: str, skip: int) -> Dict[str, float]:
    """WCE / MED / MRED / ER of the relaxed output in its number format (LSB units), on the application stream."""
    n = min(len(exact), len(approx))
    e = np.array([decode(bench_id, r[wire]) for r in exact[skip:n]], dtype=object).astype(np.float64)
    a = np.array([decode(bench_id, r[wire]) for r in approx[skip:n]], dtype=object).astype(np.float64)
    d = np.abs(e - a)
    nz = e != 0
    return {
        "wce": float(d.max()) if d.size else 0.0,
        "med": float(d.mean()) if d.size else 0.0,
        "mred": float((d[nz] / np.abs(e[nz])).mean()) if nz.any() else 0.0,
        "er": float((d > 0).mean()) if d.size else 0.0,
        "samples": int(d.size),
        "format": "%s%d" % OUTPUT_FORMAT.get(bench_id, ("unsigned", 0)) if bench_id in OUTPUT_FORMAT else "unsigned",
    }


# --------------------------------------------------------------------------- metrics helpers
def _sqnr_db(exact: np.ndarray, approx: np.ndarray) -> float:
    err = np.sum((exact - approx) ** 2, dtype=np.float64)
    sig = np.sum(exact.astype(np.float64) ** 2)
    if err == 0:
        return float("inf")
    return 10.0 * math.log10(sig / err) if sig > 0 else float("-inf")


def _psnr_db(exact: np.ndarray, approx: np.ndarray, peak: float) -> float:
    mse = np.mean((exact.astype(np.float64) - approx) ** 2)
    return float("inf") if mse == 0 else 10.0 * math.log10(peak * peak / mse)


def _verdict(value: float, threshold: Optional[float], higher_better: bool = True) -> Optional[bool]:
    if threshold is None:
        return None
    return value >= threshold if higher_better else value <= threshold


# --------------------------------------------------------------------------- stimulus
def build_stimulus(bench_id: str) -> Tuple[List[Dict[str, int]], Dict[str, Any]]:
    """Return (vectors, ctx). vectors: one dict {input_port: value} per cycle."""
    rnd = random.Random(SEED)
    ctx: Dict[str, Any] = {}

    if bench_id in ARITH:
        a, b = ("IN1", "IN2") if bench_id.startswith("multiplier") else ("X", "Y")
        w = 32 if "32b" in bench_id else 16
        vecs = [{a: rnd.getrandbits(w), b: rnd.getrandbits(w)} for _ in range(3000)]
        ctx["note"] = "uniform random operands"
        return vecs, ctx

    if bench_id == "fir_filter":
        n = np.arange(4000)
        sig = 128 + 50 * np.sin(2 * np.pi * 0.011 * n) + 30 * np.sin(2 * np.pi * 0.047 * n) + 20 * np.sin(2 * np.pi * 0.19 * n)
        sig = sig + np.random.default_rng(SEED).normal(0, 5, n.size)
        x = np.clip(np.round(sig), 0, 255).astype(int)
        ctx["note"] = "8-bit sum of 3 sinusoids (f=0.011/0.047/0.19 fs) + Gaussian noise (sigma=5)"
        return [{"x": int(v)} for v in x], ctx

    if bench_id == "sobel_filter":
        img = _gray(128)
        vecs = []
        for r in range(1, 127):
            for c in range(1, 127):
                win = img[r - 1:r + 2, c - 1:c + 2].flatten()
                vecs.append({f"p{i}": int(win[i]) for i in (0, 1, 2, 3, 5, 6, 7, 8)})
        ctx["note"] = "peppers.png grayscale 128x128, all interior 3x3 windows"
        return vecs, ctx

    if bench_id == "sobel_npu":
        img = _gray(32)
        q = 9
        hidden_w = [sm_encode(rnd.uniform(-1, 1), q, 16) for _ in range(9)]
        weight_in = [sm_encode(rnd.uniform(-1, 1), q, 16) for _ in range(80)]
        hw, wi = _pack(hidden_w, 16), _pack(weight_in, 16)
        vecs = []
        for r in range(1, 31):
            for c in range(1, 31):
                win = img[r - 1:r + 2, c - 1:c + 2].flatten()
                syn = [sm_encode(p / 255.0, q, 16) for p in win] + [sm_encode(1.0, q, 16)]
                vecs.append({"synapse_in": _pack(syn, 16), "weight_in": wi, "hidden_weight": hw})
        ctx["note"] = "peppers.png 32x32 3x3 windows (Q9, bias=1.0); one fixed seeded random weight set in [-1,1] (no trained weights in AxBench)"
        return vecs, ctx

    if bench_id == "neural_network":
        q = 9
        vecs = []
        for _ in range(3000):
            syn = [sm_encode(rnd.uniform(-1, 1), q, 16) for _ in range(8)]
            wts = [sm_encode(rnd.uniform(-1, 1), q, 16) for _ in range(8)]
            vecs.append({"synapse_in": _pack(syn, 16), "weight_in": _pack(wts, 16)})
        ctx["note"] = "seeded random inputs and weights in [-1,1] (Q9); no dataset in AxBench"
        return vecs, ctx

    if bench_id == "kinematics_fwd":
        vecs = [{"theta1": sm_encode(rnd.uniform(0, math.pi / 2), 15, 32),
                 "theta2": sm_encode(rnd.uniform(0, math.pi / 2), 15, 32)} for _ in range(2000)]
        ctx["note"] = "joint angles uniform in [0, pi/2] rad, sign-magnitude Q15"
        return vecs, ctx

    if bench_id == "kinematics_inv":
        vecs = []
        for _ in range(2000):
            t1, t2 = rnd.uniform(0.2, math.pi / 2 - 0.2), rnd.uniform(0.2, math.pi / 2 - 0.2)
            x = 11 * math.cos(t1) + 12 * math.cos(t1 + t2)
            y = 11 * math.sin(t1) + 12 * math.sin(t1 + t2)
            vecs.append({"x": sm_encode(x, 15, 32), "y": sm_encode(y, 15, 32)})
        ctx["note"] = "reachable targets from joint angles uniform in [0.2, pi/2-0.2] rad (arm lengths 11, 12), Q15"
        return vecs, ctx

    if bench_id == "kmeans_clustering":
        vecs = [{"A": a, "B": b} for a in range(256) for b in range(256)]
        ctx["note"] = "exhaustive 8-bit operand table of sub_8b used as the |a-b| kernel of 8-cluster k-means on peppers.png RGB 128x128"
        return vecs, ctx

    raise KeyError(bench_id)


def power_vectors(bench_id: str, vectors: List[Dict[str, int]]) -> List[Dict[str, int]]:
    """Stimulus for power simulation: the application stream, capped at POWER_VECTORS cycles."""
    if bench_id == "kmeans_clustering":
        # realistic (pixel, centroid) pairs from exact k-means instead of the exhaustive table
        img = _rgb(128).reshape(-1, 3)
        _, cents = _kmeans(img, lambda a, b: np.abs(a - b))
        rnd = random.Random(SEED)
        out = []
        for _ in range(POWER_VECTORS):
            p = img[rnd.randrange(len(img))]
            c = cents[rnd.randrange(len(cents))]
            ch = rnd.randrange(3)
            out.append({"A": int(p[ch]), "B": int(c[ch])})
        return out
    if len(vectors) <= POWER_VECTORS:
        return vectors
    step = len(vectors) / POWER_VECTORS
    return [vectors[int(i * step)] for i in range(POWER_VECTORS)]


# --------------------------------------------------------------------------- k-means
def _kmeans(pixels: np.ndarray, dist, k: int = 8, iters: int = 10) -> Tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(SEED)
    cents = pixels[rng.choice(len(pixels), k, replace=False)].astype(np.int64)
    labels = np.zeros(len(pixels), dtype=np.int64)
    for _ in range(iters):
        d = np.stack([dist(pixels, c[None, :]).sum(axis=1) for c in cents], axis=1)
        labels = np.argmin(d, axis=1)
        for j in range(k):
            m = pixels[labels == j]
            if len(m):
                cents[j] = np.round(m.mean(axis=0)).astype(np.int64)
    return labels, cents


# --------------------------------------------------------------------------- metrics
def app_metrics(bench_id: str, vectors: List[Dict[str, int]], exact: List[Dict[str, int]], approx: List[Dict[str, int]],
                relax_wire: str, skip: int) -> Dict[str, Any]:
    n = min(len(exact), len(approx))
    e_rows, a_rows = exact[skip:n], approx[skip:n]

    def col(rows, name):
        return np.array([r[name] for r in rows], dtype=object)

    if bench_id in ARITH:
        e = np.array([decode(bench_id, int(v)) for v in col(e_rows, relax_wire)], dtype=np.float64)
        a = np.array([decode(bench_id, int(v)) for v in col(a_rows, relax_wire)], dtype=np.float64)
        width = {"S": 33 if "32b" in bench_id else 17, "P": 64 if "32b" in bench_id else 32}[relax_wire]
        err = np.abs(e - a)
        nz = e != 0
        e_abs = np.abs(e)
        thr = 25.0 if bench_id.startswith("multiplier") else 30.0
        sq = _sqnr_db(e, a)
        return {"name": "SQNR", "value": sq, "unit": "dB", "threshold": f">= {thr} dB", "pass": _verdict(sq, thr),
                "extra": {"NMED": float(err.mean() / (2 ** width - 1)), "MRED_pct": float((err[nz] / e_abs[nz]).mean() * 100) if nz.any() else 0.0}}

    if bench_id == "fir_filter":
        e = col(e_rows, "dataout").astype(np.float64)
        a = col(a_rows, "dataout").astype(np.float64)
        snr = _sqnr_db(e, a)
        return {"name": "Output SNR", "value": snr, "unit": "dB", "threshold": ">= 25.0 dB", "pass": _verdict(snr, 25.0),
                "extra": {"mean_abs_err": float(np.abs(e - a).mean())}}

    if bench_id == "sobel_filter":
        e = col(e_rows, "out").astype(np.float64)
        a = col(a_rows, "out").astype(np.float64)
        ps = _psnr_db(e, a, 255.0)
        return {"name": "Edge-image PSNR", "value": ps, "unit": "dB", "threshold": ">= 30.0 dB", "pass": _verdict(ps, 30.0),
                "extra": {"pixels_differing_pct": float((e != a).mean() * 100)}}

    if bench_id == "sobel_npu":
        e = np.array([sm_decode(int(v), 9, 16) for v in col(e_rows, "synapse_out")])
        a = np.array([sm_decode(int(v), 9, 16) for v in col(a_rows, "synapse_out")])
        peak = float(e.max() - e.min()) or 1.0
        ps = _psnr_db(e, a, peak)
        return {"name": "Output-image PSNR", "value": ps, "unit": "dB", "threshold": ">= 30.0 dB", "pass": _verdict(ps, 30.0),
                "extra": {"SQNR_dB": _sqnr_db(e, a), "peak": peak}}

    if bench_id == "neural_network":
        e = np.array([sm_decode(int(v), 9, 16) for v in col(e_rows, "synapse_out")])
        a = np.array([sm_decode(int(v), 9, 16) for v in col(a_rows, "synapse_out")])
        thr = 0.5 if (e.min() >= 0 and e.max() <= 1.0) else 0.0
        agree = float(((e >= thr) == (a >= thr)).mean() * 100)
        return {"name": "Decision agreement", "value": agree, "unit": "%", "threshold": ">= 99.0 %", "pass": _verdict(agree, 99.0),
                "extra": {"SQNR_dB": _sqnr_db(e, a), "decision_threshold": thr}}

    if bench_id == "kinematics_fwd":
        e = np.array([sm_decode(int(v), 15, 32) for v in col(e_rows, "x")])
        a = np.array([sm_decode(int(v), 15, 32) for v in col(a_rows, "x")])
        d = np.abs(e - a)
        return {"name": "End-effector |dx| (mean)", "value": float(d.mean()), "unit": "arm units", "threshold": "<= 0.05 arm units",
                "pass": _verdict(float(d.mean()), 0.05, higher_better=False),
                "extra": {"max_abs_dx": float(d.max()), "rmse_dx": float(np.sqrt((d ** 2).mean()))}}

    if bench_id == "kinematics_inv":
        e = np.array([sm_decode(int(v), 15, 32) for v in col(e_rows, "theta1")])
        a = np.array([sm_decode(int(v), 15, 32) for v in col(a_rows, "theta1")])
        d = np.abs(e - a)
        rmse = float(np.sqrt((d ** 2).mean()))
        return {"name": "theta1 RMSE", "value": rmse, "unit": "rad", "threshold": "<= 0.05 rad", "pass": _verdict(rmse, 0.05, higher_better=False),
                "extra": {"max_abs_dtheta1": float(d.max())}}

    if bench_id == "kmeans_clustering":
        def table(rows):
            t = np.zeros((256, 256), dtype=np.int64)
            for v, r in zip(vectors, rows):
                s = r["S"] & 0x1FF
                t[v["A"], v["B"]] = (512 - s) if s & 0x100 else s
            return t
        te, ta = table(exact[:n]), table(approx[:n])
        img = _rgb(128).reshape(-1, 3)
        le, ce = _kmeans(img, lambda p, c: te[p, np.broadcast_to(c, p.shape)])
        la, ca = _kmeans(img, lambda p, c: ta[p, np.broadcast_to(c, p.shape)])
        mis = float((le != la).mean() * 100)
        ps = _psnr_db(ce[le].astype(np.float64), ca[la].astype(np.float64), 255.0)
        return {"name": "Pixel misclassification", "value": mis, "unit": "%", "threshold": "<= 2.0 %", "pass": _verdict(mis, 2.0, higher_better=False),
                "extra": {"clustered_image_PSNR_dB": ps}}

    raise KeyError(bench_id)
