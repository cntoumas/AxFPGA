"""Bit-accurate Python models of the Phase-4 designs (benchmarks/{conv2d,iir,image_pipeline,fft64}).

Each model follows the RTL register by register, so the iverilog tests can compare the RTL with it cycle by cycle;
the pure-Python tests compare the models with floating-point references (numpy FFT, filter recurrence, image
convolutions) to check the algorithms themselves.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple


def wrap(v: int, w: int) -> int:
    """Two's complement value of the low w bits of v."""
    v &= (1 << w) - 1
    return v - (1 << w) if v >> (w - 1) else v


# ---------------------------------------------------------------------------------------------------------------
# conv2d_3x3: act = sat255(ReLU(sum_i win_i * w_i) >> SHIFT); out row t + 1 holds the result of vector t.
# ---------------------------------------------------------------------------------------------------------------
def conv2d_act(win: Sequence[int], weights: Sequence[int], shift: int = 6) -> int:
    acc = sum(int(p) * int(w) for p, w in zip(win, weights))
    if acc < 0:
        return 0
    return min(255, acc >> shift)


# ---------------------------------------------------------------------------------------------------------------
# iir_biquad: y[n] = sat16((329 x[n] + 658 x[n-1] + 329 x[n-2] + 25576 y[n-1] - 10508 y[n-2]) >> 14)
# out row n holds y[n] (y_out is the y[n-1] register after the clock edge of sample n).
# ---------------------------------------------------------------------------------------------------------------
IIR_B = (329, 658, 329)
IIR_A = (16384, -25576, 10508)


def iir_model(x: Sequence[int]) -> List[int]:
    x1 = x2 = y1 = y2 = 0
    out = []
    for x0 in x:
        acc = IIR_B[0] * x0 + IIR_B[1] * x1 + IIR_B[2] * x2 - IIR_A[1] * y1 - IIR_A[2] * y2
        y = max(-32768, min(32767, acc >> 14))
        x2, x1, y2, y1 = x1, x0, y1, y
        out.append(y)
    return out


# ---------------------------------------------------------------------------------------------------------------
# image_pipeline: luma -> 3x3 Gaussian blur -> Sobel magnitude, shift-register line buffers of WIDTH pixels.
# One call per valid cycle; returns edge_out after that clock edge (all registers start at 0 after reset).
# ---------------------------------------------------------------------------------------------------------------
class ImagePipelineModel:
    def __init__(self, width: int = 64):
        self.w = width
        self.luma = 0
        self.lb1a = [0] * width        # index 0 = oldest (top of the RTL vector)
        self.lb1b = [0] * width
        self.a = [[0] * 3 for _ in range(3)]     # a[row][col]: row 0 oldest line, col 2 newest pixel
        self.blur = 0
        self.lb2a = [0] * width
        self.lb2b = [0] * width
        self.s = [[0] * 3 for _ in range(3)]
        self.edge = 0

    @staticmethod
    def luma_of(rgb: int) -> int:
        r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
        return ((77 * r + 150 * g + 29 * b) & 0xFFFF) >> 8

    @staticmethod
    def blur_of(a) -> int:
        s = (a[0][0] + 2 * a[0][1] + a[0][2] + 2 * a[1][0] + 4 * a[1][1] + 2 * a[1][2] + a[2][0] + 2 * a[2][1] + a[2][2])
        return (s & 0xFFF) >> 4

    @staticmethod
    def edge_of(s) -> int:
        gx = (s[0][2] + 2 * s[1][2] + s[2][2]) - (s[0][0] + 2 * s[1][0] + s[2][0])
        gy = (s[2][0] + 2 * s[2][1] + s[2][2]) - (s[0][0] + 2 * s[0][1] + s[0][2])
        mag4 = (abs(gx) + abs(gy)) >> 2
        return min(255, mag4)

    def step(self, rgb: int) -> int:
        # values computed from the registers before the edge
        luma_n = self.luma_of(rgb)
        l1, l2 = self.lb1a[0], self.lb1b[0]
        blur_n = self.blur_of(self.a)
        m1, m2 = self.lb2a[0], self.lb2b[0]
        edge_n = self.edge_of(self.s)
        # clock edge
        self.lb1a = self.lb1a[1:] + [self.luma]
        self.lb1b = self.lb1b[1:] + [l1]
        for row, new in zip(self.a, (l2, l1, self.luma)):
            row[0], row[1], row[2] = row[1], row[2], new
        self.lb2a = self.lb2a[1:] + [self.blur]
        self.lb2b = self.lb2b[1:] + [m1]
        for row, new in zip(self.s, (m2, m1, self.blur)):
            row[0], row[1], row[2] = row[1], row[2], new
        self.luma, self.blur, self.edge = luma_n, blur_n, edge_n
        return self.edge


IMAGE_LATENCY_EXTRA = 6     # out row t holds the edge pixel of raster index t - 2 * WIDTH - 6


# ---------------------------------------------------------------------------------------------------------------
# fft64_psd: six radix-2 SDF stages (delay 32 .. 1), Q1.14 twiddles, halving butterflies, power of each bin.
# ---------------------------------------------------------------------------------------------------------------
TWIDDLES = [(round(16384 * math.cos(2 * math.pi * n / 64)), round(16384 * math.sin(2 * math.pi * n / 64)))
            for n in range(32)]


def sdf_stage(samples: Sequence[Tuple[int, int]], logd: int) -> List[Tuple[int, int]]:
    """Valid outputs of one stage for a stream of valid inputs (out_valid = in_valid & (phase | started))."""
    d = 1 << logd
    cnt, started = 0, False
    dl = [(0, 0)] * d                    # index 0 = oldest (top of the RTL vector)
    out = []
    for br, bi in samples:
        phase = (cnt >> logd) & 1
        dr, di = dl[0]
        sr, si = (dr + br) >> 1, (di + bi) >> 1
        fr, fi = (dr - br) >> 1, (di - bi) >> 1
        dl_in = (fr, fi) if phase else (br, bi)
        o_re, o_im = (sr, si) if phase else (dr, di)
        k = cnt & (d - 1)
        n = 0 if phase else (k << (5 - logd)) & 31
        c, s = TWIDDLES[n]
        t_re = wrap((o_re * c + o_im * s) >> 14, 16)
        t_im = wrap((o_im * c - o_re * s) >> 14, 16)
        if phase or started:
            out.append((t_re, t_im))
        if phase:
            started = True
        dl = dl[1:] + [dl_in]
        cnt = (cnt + 1) & (2 * d - 1)
    return out


def fft64_model(samples: Sequence[Tuple[int, int]]) -> List[int]:
    """Valid out_pow values (bins of each 64-sample block in bit-reversed order) for a stream of valid inputs."""
    s = list(samples)
    for logd in (5, 4, 3, 2, 1, 0):
        s = sdf_stage(s, logd)
    return [r * r + i * i for r, i in s]


def bitrev6(j: int) -> int:
    return int(f"{j:06b}"[::-1], 2)


def fft64_ports(rows: List[Dict[str, int]]) -> List[int]:
    """out_pow of the rows where out_valid is 1 (iverilog output rows)."""
    return [r["out_pow"] for r in rows if r["out_valid"]]
