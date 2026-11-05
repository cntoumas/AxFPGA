#!/usr/bin/env python3
"""Reproducibility manifest of a suite run: stripped results.json + mapped netlists, SHA-256.

Two runs are reproducible when their manifests are identical. Stripping removes what is
expected to differ between runs: wall-clock times and machine-specific paths.

    python scripts/strip_results.py --reports RUN/reports --work RUN/work > manifest.json
    diff manifest_a.json manifest_b.json

``--reports`` holds results.json; ``--work`` is the suite --build-dir (one directory per
benchmark with exact_mapped.v / approx_mapped.v). ``--write-stripped`` also writes the
canonical stripped results next to the manifest, for inspection with diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

VOLATILE_KEY = re.compile(r"(^|_)(time|times|elapsed|wall|seconds|date|timestamp)(_|$)", re.IGNORECASE)
# Added after the Phase-0 anchor (certification derives from the formal statuses; quality / heldout are the
# per-constraint records and the held-out stream, which the anchor has no counterpart for; dsp_inference (Phase 4)
# restates the run's --dsp setting), so not compared.
IGNORED_KEYS = frozenset({"certification", "quality", "heldout", "heldout_reason", "dsp_inference"})
NETLISTS = ("exact_mapped.v", "approx_mapped.v")


def strip(obj: Any, prefixes: List[str]) -> Any:
    """Drop volatile keys; replace machine-specific path prefixes with placeholders."""
    if isinstance(obj, dict):
        return {k: strip(v, prefixes) for k, v in obj.items() if not VOLATILE_KEY.search(k) and k not in IGNORED_KEYS}
    if isinstance(obj, list):
        return [strip(v, prefixes) for v in obj]
    if isinstance(obj, str):
        for i, p in enumerate(prefixes):
            obj = obj.replace(p, f"<PATH{i}>")
        return obj
    return obj


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest(reports: Path, work: Path | None, extra_prefixes: List[str]) -> Dict[str, Any]:
    prefixes = [str(p) for p in ([work.resolve()] if work else []) + [reports.resolve(), Path(__file__).resolve().parent.parent]]
    prefixes += extra_prefixes + [str(Path.home())]
    results = json.loads((reports / "results.json").read_text())
    stripped = canonical(strip(results, prefixes))
    out: Dict[str, Any] = {"results.json": sha256_text(stripped), "benchmarks": sorted(r["id"] for r in results), "netlists": {}}
    if work is not None:
        for r in sorted(results, key=lambda r: r["id"]):
            for name in NETLISTS:
                f = work / r["id"] / name
                out["netlists"][f"{r['id']}/{name}"] = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else None
    return {"manifest": out, "stripped": stripped}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reports", required=True, type=Path, help="directory with results.json")
    ap.add_argument("--work", type=Path, default=None, help="suite --build-dir with per-benchmark mapped netlists")
    ap.add_argument("--prefix", action="append", default=[], help="extra path prefix to strip (repeatable)")
    ap.add_argument("--write-stripped", type=Path, default=None, help="also write the stripped results.json here")
    args = ap.parse_args()
    m = manifest(args.reports, args.work, args.prefix)
    if args.write_stripped:
        args.write_stripped.write_text(m["stripped"])
    sys.stdout.write(canonical(m["manifest"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
