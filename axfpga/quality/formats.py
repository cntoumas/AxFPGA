"""Number formats of a relaxed output word: unsigned, two's complement, sign-magnitude, optional fractional bits.

``decode`` reproduces scripts/axbench_apps.decode bit for bit; ``decode_real`` / ``encode`` reproduce
sm_decode / sm_encode for the sign-magnitude kind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

from .errors import QualityError

_FMT = re.compile(r"^(sm|s|u)(\d+)?(?:q(\d+))?$")


@dataclass(frozen=True)
class NumberFormat:
    kind: str                       # "u" | "s" | "sm"
    width: Optional[int] = None     # None until bound to a wire
    frac: int = 0

    @classmethod
    def parse(cls, text: str, wire: str = "") -> "NumberFormat":
        m = _FMT.match(str(text).strip().lower())
        if not m:
            raise QualityError(f"axc_format of '{wire}': cannot parse '{text}'; expected u|s|sm [width] [q frac], e.g. 'sm16q9'")
        kind = m.group(1)
        width = int(m.group(2)) if m.group(2) is not None else None
        frac = int(m.group(3)) if m.group(3) is not None else 0
        if width is not None and width < 1:
            raise QualityError(f"axc_format of '{wire}': cannot parse '{text}'; expected u|s|sm [width] [q frac], e.g. 'sm16q9'")
        if width is not None and frac > width:
            raise QualityError(f"axc_format of '{wire}': fractional bits {frac} exceed the width {width}")
        return cls(kind, width, frac)

    def bind(self, wire_width: int, wire: str = "") -> "NumberFormat":
        """Fill in the width (default: the wire width) and check it against the wire."""
        w = self.width if self.width is not None else int(wire_width)
        if w > wire_width:
            raise QualityError(f"axc_format of '{wire}': width {w} exceeds the wire width {wire_width}")
        if self.frac > w:
            raise QualityError(f"axc_format of '{wire}': fractional bits {self.frac} exceed the width {w}")
        return replace(self, width=w)

    def _w(self) -> int:
        if self.width is None:
            raise QualityError(f"number format '{self}' is not bound to a wire width")
        return self.width

    def decode(self, value: int) -> int:
        """Integer value in LSB units. 'u' returns the word unmasked (legacy behaviour)."""
        value = int(value)
        if self.kind == "s":
            w = self._w()
            value &= (1 << w) - 1
            return value - (1 << w) if value >> (w - 1) else value
        if self.kind == "sm":
            w = self._w()
            mag = value & ((1 << (w - 1)) - 1)
            return -mag if (value >> (w - 1)) & 1 else mag
        return value

    def decode_real(self, value: int) -> float:
        return self.decode(value) / float(1 << self.frac)

    def encode(self, x: float) -> int:
        """Word for the real value x. 'sm': exactly sm_encode; 's'/'u': round and saturate."""
        w = self._w()
        if self.kind == "sm":
            mag = min(int(round(abs(x) * (1 << self.frac))), (1 << (w - 1)) - 1)
            return ((1 << (w - 1)) | mag) if x < 0 and mag else mag
        q = int(round(x * (1 << self.frac)))
        if self.kind == "u":
            return max(0, min(q, (1 << w) - 1))
        q = max(-(1 << (w - 1)), min(q, (1 << (w - 1)) - 1))
        return q & ((1 << w) - 1)

    @property
    def peak(self) -> float:
        w = self._w()
        return float((1 << w) - 1) if self.kind == "u" else float((1 << (w - 1)) - 1)

    def __str__(self) -> str:
        return f"{self.kind}{'' if self.width is None else self.width}{'q%d' % self.frac if self.frac else ''}"
