"""Yosys command line on top of the pyosys wheel (``libyosys``, Yosys 0.70)."""

from __future__ import annotations

import os
import sys
from typing import List, Optional


def _usage() -> str:
    return "usage: axfpga-yosys [-q] [-V] [-p COMMANDS]... [-s SCRIPT]... [FILE]..."


def _quote(path: str) -> str:
    return f'"{path}"' if any(c.isspace() for c in path) else path


def _reader(path: str) -> str:
    """Frontend for an input file, as the Yosys driver's ``auto`` frontend picks it."""
    low = path.lower()
    if low.endswith(".ys"):
        return "script"
    if low.endswith(".il") or low.endswith(".rtlil"):
        return "read_rtlil"
    if low.endswith(".json"):
        return "read_json"
    if low.endswith(".sv"):
        return "read_verilog -sv"
    return "read_verilog"


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    quiet = False
    commands: List[str] = []
    scripts: List[str] = []
    files: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-q":
            quiet = True
        elif a == "-V":
            from pyosys import libyosys as ys
            print(ys.yosys_maybe_version())
            return 0
        elif a in ("-p", "-s"):
            if i + 1 >= len(args):
                print(f"ERROR: option {a} needs an argument\n{_usage()}", file=sys.stderr)
                return 1
            (commands if a == "-p" else scripts).append(args[i + 1])
            i += 1
        elif a.startswith("-"):
            print(f"ERROR: unsupported option {a}\n{_usage()}", file=sys.stderr)
            return 1
        else:
            files.append(a)
        i += 1

    saved_stdout = None
    if quiet:
        # libyosys logs to the process stdout: send it to /dev/null, keep stderr for errors.
        sys.stdout.flush()
        saved_stdout = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)

    status = 0
    try:
        from pyosys import libyosys as ys
        design = ys.Design()
        for f in files:
            ys.run_pass(f"{_reader(f)} {_quote(f)}", design)
        for s in scripts:
            ys.run_pass(f"script {_quote(s)}", design)
        for c in commands:
            ys.run_pass(c, design)
    except Exception as exc:  # libyosys raises on log_error / command errors
        status = 1
        msg = str(exc).strip() or exc.__class__.__name__
        print(f"ERROR: {msg}", file=sys.stderr)
    finally:
        try:
            from pyosys import libyosys as ys
            ys.log_flush()
        except Exception:
            pass
        sys.stdout.flush()
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
    return status


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Skip libyosys static destructors (slow on large designs); all output is flushed above.
    os._exit(code)
