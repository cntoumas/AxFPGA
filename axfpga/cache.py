"""Content-hash cache for the subprocess-based evaluation steps (Yosys, iverilog/vvp, formal checks).

An entry is keyed by a SHA-256 over everything the step depends on: the bytes of its input files, its
scalar options and the tool fingerprint (pyosys version, ``iverilog -V``, the Xilinx cell models). A hit
returns the stored result and restores the stored output files, so a warm run is bit-identical to a
cold one. Results that depend on machine load (TIMEOUT, ERROR, COMPILATION_ERROR) are never stored.

Location: ``$AXFPGA_CACHE`` or ``./build/.cache``. ``AXFPGA_NO_CACHE=1`` (or ``--no-cache`` on the
suite) turns it off. Writes are atomic (temp directory, then rename), so concurrent threads and
processes can share the directory.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import pickle
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

FORMAT = "1"                    # bump to invalidate every entry
MAX_FILE_BYTES = 256 * 1024 * 1024   # larger output files (VCD) are not cached
UNCACHEABLE_STATUS = {"TIMEOUT", "ERROR", "COMPILATION_ERROR", "INCONCLUSIVE"}

_lock = threading.Lock()
_stats: Dict[str, Dict[str, int]] = {}
_fingerprint: Optional[str] = None


def enabled() -> bool:
    return os.environ.get("AXFPGA_NO_CACHE", "").strip().lower() not in ("1", "true", "yes", "on")


def disable() -> None:
    os.environ["AXFPGA_NO_CACHE"] = "1"


def root() -> Path:
    return Path(os.environ.get("AXFPGA_CACHE") or (Path.cwd() / "build" / ".cache")).expanduser().resolve()


def stats() -> Dict[str, Dict[str, int]]:
    """{namespace: {hits, misses}} of this process."""
    with _lock:
        return {k: dict(v) for k, v in _stats.items()}


def reset_stats() -> None:
    with _lock:
        _stats.clear()


def summary() -> str:
    s = stats()
    if not s:
        return "cache: unused"
    return "cache: " + ", ".join(f"{k} {v['hits']} hit / {v['misses']} miss" for k, v in sorted(s.items()))


def _count(ns: str, what: str) -> None:
    with _lock:
        _stats.setdefault(ns, {"hits": 0, "misses": 0})[what] += 1


def tool_fingerprint() -> str:
    """Versions of every external tool a cached result can depend on."""
    global _fingerprint
    if _fingerprint is None:
        from importlib import metadata

        parts = [FORMAT]
        try:
            parts.append("pyosys=" + metadata.version("pyosys"))
        except Exception:
            parts.append("pyosys=?")
        parts.append("yosys=" + os.environ.get("AXFPGA_YOSYS", ""))
        try:
            parts.append("iverilog=" + subprocess.run(["iverilog", "-V"], capture_output=True, text=True).stdout.splitlines()[0])
        except Exception:
            parts.append("iverilog=?")
        try:
            from axfpga.config import xilinx_cells_sim

            cells = xilinx_cells_sim()
            parts.append("cells_sim=" + (hashlib.sha256(cells.read_bytes()).hexdigest() if cells.exists() else "none"))
        except Exception:
            parts.append("cells_sim=?")
        _fingerprint = "|".join(parts)
    return _fingerprint


_SRC_ATTR = re.compile(rb'src = "[^"]*"')
_EMBEDDED_PATH = re.compile(r"(?:/[\w.\-@+]+)+/([\w.\-]+\.v)")


def file_digest(path: Any) -> str:
    """SHA-256 of a file. For Verilog the ``src = "<path>:<line>"`` attributes Yosys writes are blanked: they carry
    the machine-specific work-directory path of the run that produced the file and do not change any result."""
    p = Path(path)
    if not p.is_file():
        return "missing"
    h = hashlib.sha256()
    if p.suffix == ".v" and p.stat().st_size <= 64 * 1024 * 1024:
        h.update(_SRC_ATTR.sub(b'src = ""', p.read_bytes()))
        return h.hexdigest()
    if p.suffix == ".json" and p.stat().st_size <= 64 * 1024 * 1024:
        try:                                    # metadata files embed absolute work-directory paths
            h.update(stable_repr(json.loads(p.read_text())).encode())
            return h.hexdigest()
        except ValueError:
            pass
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_repr(obj: Any) -> str:
    """repr of a nested structure without machine-specific paths (Path -> file name, absolute str -> basename)."""
    if isinstance(obj, Path):
        return f"<path:{obj.name}>"
    if isinstance(obj, str):
        if obj.startswith("/"):
            return f"<abs:{os.path.basename(obj)}>"
        return repr(_EMBEDDED_PATH.sub(r"\1", obj))      # Yosys names such as $add$/work/x.v:59$17
    if isinstance(obj, dict):
        return "{" + ",".join(f"{stable_repr(k)}:{stable_repr(v)}" for k, v in sorted(obj.items(), key=lambda kv: repr(kv[0]))) + "}"
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = sorted(obj, key=repr) if isinstance(obj, (set, frozenset)) else obj
        return "[" + ",".join(stable_repr(v) for v in items) + "]"
    return repr(obj)


def make_key(ns: str, files: Iterable[Any] = (), **parts: Any) -> str:
    """Key of a step: namespace, tool fingerprint, input file contents (in the given order) and scalar options."""
    h = hashlib.sha256()
    h.update(ns.encode())
    h.update(b"\0" + tool_fingerprint().encode())
    dbg = []
    for f in files:
        d = file_digest(f)
        h.update(b"\0F" + d.encode())
        dbg.append(f"F {Path(f).name}={d[:10]}")
    for k in sorted(parts):
        pr = stable_repr(parts[k])
        h.update(b"\0P" + k.encode() + b"=" + pr.encode())
        dbg.append(f"P {k}={hashlib.sha256(pr.encode()).hexdigest()[:10]}")
    key = h.hexdigest()
    if os.environ.get("AXFPGA_CACHE_DEBUG"):
        logger.warning("cache key %s %s: %s", ns, key[:10], " ".join(dbg))
    return key


def _entry_dir(ns: str, key: str) -> Path:
    return root() / ns / key[:2] / key


def load(ns: str, key: str) -> Optional[Tuple[Any, Path]]:
    """(value, files directory) of an entry, or None. Counts a hit or a miss."""
    if not enabled():
        return None
    d = _entry_dir(ns, key)
    try:
        with open(d / "value.pkl", "rb") as f:
            value = pickle.load(f)
    except Exception:
        _count(ns, "misses")
        return None
    _count(ns, "hits")
    return value, d / "files"


def restore(files_dir: Path, name: str, dest: Any) -> bool:
    src = files_dir / name
    if not src.is_file():
        return False
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return True


def save(ns: str, key: str, value: Any, files: Optional[Mapping[str, Any]] = None) -> None:
    """Store ``value`` and the named output files (name -> path). Skipped when a file is missing or too large."""
    if not enabled():
        return
    files = files or {}
    for p in files.values():
        if not Path(p).is_file() or Path(p).stat().st_size > MAX_FILE_BYTES:
            return
    d = _entry_dir(ns, key)
    if (d / "value.pkl").exists():
        return
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp_", dir=d.parent))
        (tmp / "files").mkdir()
        for name, p in files.items():
            shutil.copyfile(p, tmp / "files" / name)
        with open(tmp / "value.pkl", "wb") as f:
            pickle.dump(value, f, protocol=4)
        try:
            os.rename(tmp, d)
        except OSError:                          # another writer got there first
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:                     # the cache must never break a run
        logger.debug("cache save failed for %s: %s", ns, exc)


def object_parts(obj: Any, skip: Iterable[str] = ()) -> Dict[str, Any]:
    """Key parts of an object's attributes: files by content, everything else by repr (``meta`` style dicts are
    derived from files and skipped by name)."""
    parts: Dict[str, Any] = {}
    for k, v in sorted(vars(obj).items()):
        if k in skip:
            continue
        if isinstance(v, Path):
            parts[k] = file_digest(v)
        else:
            parts[k] = v
    return parts


def cached_verify(ns: str, skip: Iterable[str] = ("meta",)) -> Callable:
    """Decorator for ``verify(self)`` methods: cache the returned dict, except load-dependent outcomes."""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(self: Any, *a: Any, **k: Any) -> Any:
            if not enabled():
                return fn(self, *a, **k)
            key = make_key(f"{ns}.{type(self).__name__}", **object_parts(self, skip))
            hit = load(ns, key)
            if hit is not None:
                return hit[0]
            res = fn(self, *a, **k)
            if isinstance(res, dict) and str(res.get("status", "")) not in UNCACHEABLE_STATUS and not _has_bad_status(res):
                save(ns, key, res)
            return res

        return wrapper

    return deco


def _has_bad_status(res: Mapping[str, Any]) -> bool:
    for v in res.values():
        s = getattr(v, "value", v)
        if isinstance(s, str) and s in UNCACHEABLE_STATUS:
            return True
    return False
