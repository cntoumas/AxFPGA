"""AxBench k-means: the exhaustive 8-bit operand table of sub_8b is the |a-b| kernel of 8-cluster k-means on an RGB image."""

import importlib.util as _u
import pathlib as _p
import random

import numpy as np

from axfpga.quality import Stimulus, derive_seed, psnr_db

_s = _u.spec_from_file_location("axbench_common", _p.Path(__file__).with_name("_common.py"))
C = _u.module_from_spec(_s)
_s.loader.exec_module(C)


def _kmeans(pixels, dist, seed, k=8, iters=10):
    rng = np.random.default_rng(seed)
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


def stimulus(stream, ctx):
    # the table is the same for both streams; the streams differ in the image the metric clusters
    vecs = [{"A": a, "B": b} for a in range(256) for b in range(256)]
    return Stimulus(vecs, "exhaustive 8-bit operand table of sub_8b used as the |a-b| kernel of 8-cluster k-means on "
                          "peppers.png RGB 128x128" if stream == "tuning" else
                    "exhaustive 8-bit operand table of sub_8b, k-means on baboon.gaussian.jpg RGB 128x128")


def power_stimulus(vectors, ctx):
    """Realistic (pixel, centroid) pairs from exact k-means instead of the exhaustive table (always the tuning image)."""
    img = C.rgb("tuning", 128).reshape(-1, 3)
    _, cents = _kmeans(img, lambda a, b: np.abs(a - b), ctx.seed)
    rnd = random.Random(ctx.seed)
    out = []
    for _ in range(2000):
        p = img[rnd.randrange(len(img))]
        c = cents[rnd.randrange(len(cents))]
        ch = rnd.randrange(3)
        out.append({"A": int(p[ch]), "B": int(c[ch])})
    return out


def metric(stream, vectors, exact, approx, ctx):
    n = min(len(exact), len(approx))
    w = ctx.relax_wire

    def table(rows):
        t = np.zeros((256, 256), dtype=np.int64)
        for v, r in zip(vectors, rows):
            s = r[w] & 0x1FF
            t[v["A"], v["B"]] = (512 - s) if s & 0x100 else s
        return t

    te, ta = table(exact[:n]), table(approx[:n])
    img = C.rgb(stream, 128).reshape(-1, 3)
    seed = derive_seed(ctx.seed, stream)
    le, ce = _kmeans(img, lambda p, c: te[p, np.broadcast_to(c, p.shape)], seed)
    la, ca = _kmeans(img, lambda p, c: ta[p, np.broadcast_to(c, p.shape)], seed)
    return {"name": "Pixel misclassification", "value": float((le != la).mean() * 100), "unit": "%",
            "extra": {"clustered_image_PSNR_dB": psnr_db(ce[le].astype(np.float64), ca[la].astype(np.float64), 255.0)}}
