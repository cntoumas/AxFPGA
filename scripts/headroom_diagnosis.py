#!/usr/bin/env python3
"""Headroom diagnosis (Phase 5): which constraint stops each design from being approximated further.

Reads the per-design ``<build-dir>/<id>/result.json`` of one or more suite runs (``--run LABEL=BUILD_DIR``, e.g.
``--run dsp_on=build/phase5/dsp_on --run dsp_off=build/phase5/dsp_off``) and writes ``headroom.json`` and
``headroom.md`` into ``--out-dir``.

Per design and run:
- ``usage``: every tuning-stream constraint as value / limit (error metrics: <= 1 passes; application metrics: the
  margin to the threshold). The largest ratio is the tightest constraint of the accepted design.
- ``limiter``: what stopped a cheaper design, one of
    wce, med, mred, er, rmse, app   a rejected, cheaper candidate failed that tuning-stream limit first
    held-out                        it passed the tuning stream but failed the held-out gate
    formal                          a formal proof failed
    cone-bound                      candidates cheaper than the exact cone exist only above the cone WCE bound
    menu                            every candidate inside the bounds was tried; none is cheaper
    search-schedule                 a deeper PPG depth exists inside the budget but the binary search did not reach it
    budget                          the largest PPG depth that fits the WCE budget was accepted
- ``detail``: the numbers behind it.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOWER_BETTER = ("wce", "med", "mred", "er", "rmse")


def usage_of(quality: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each constraint as a ratio to its limit: value / limit for '<=' (<= 1 passes), limit / value for '>=' (<= 1 passes)."""
    out = []
    for q in quality or []:
        v, t = q.get("value"), q.get("threshold")
        if v is None or t in (None, 0):
            continue
        up = q["op"] in (">=", ">")
        if v in (float("inf"), "inf"):
            ratio = 0.0
        elif up:
            ratio = (t / v) if v else float("inf")
        else:
            ratio = v / t
        out.append({"metric": q["metric"], "op": q["op"], "value": v, "limit": t, "ratio": round(ratio, 4)})
    return out


def failure_tags(reason: str) -> List[str]:
    """Constraint names named by an attempt's rejection reason, in order."""
    reason = reason or ""
    if reason.startswith("held-out"):
        return ["held-out"]
    if reason.startswith("formal"):
        return ["formal"]
    if reason.startswith("no top-level savings"):
        return ["no-savings"]
    tags = []
    for part in reason.split(";"):
        part = part.strip()
        m = re.match(r"(WCE|MED|MRED|ER|RMSE)\b", part)
        if m:
            tags.append(m.group(1).lower())
        elif "fails" in part:
            tags.append("app")
    return tags


def ppg_depth(name: str) -> Optional[int]:
    m = re.match(r"ppg_design_k(\d+)", name or "")
    return int(m.group(1)) if m else None


def diagnose(r: Dict[str, Any]) -> Dict[str, Any]:
    usage = usage_of(r.get("quality"))
    tight = max(usage, key=lambda u: u["ratio"]) if usage else None
    method = r["method"]
    attempts = r.get("attempts", [])
    tour = r.get("tournament") or {}
    entries = tour.get("entries", [])
    base_lcs = tour.get("baseline_lcs")
    lim, detail = "menu", ""
    rejected = [a for a in attempts if a.get("outcome") == "rejected"]

    if method.startswith("ppg_design_k"):
        k = ppg_depth(method)
        deeper = sorted((ppg_depth(a["candidate"]), a) for a in rejected if (ppg_depth(a["candidate"]) or 0) > k)
        if deeper:
            kd, a = deeper[0]
            tags = [t for t in failure_tags(a.get("reason", "")) if t != "no-savings"]
            lim = tags[0] if tags else "menu"
            detail = f"k={kd} rejected: {a.get('reason', '')[:110]}"
        else:
            lim = "search-schedule"
            detail = f"accepted k={k}; no deeper depth was evaluated (binary search of at most 7 steps)"
    elif not r["fell_back"]:
        champ_lcs = next((a.get("cone_lcs") for a in attempts if a.get("outcome") == "accepted"), None)
        cheaper = [a for a in rejected if champ_lcs is not None and (a.get("cone_lcs") or 10**9) < champ_lcs]
        cheaper_in_bound = [e for e in entries if e["within_bound"] and champ_lcs is not None and (e["lcs"] or 10**9) < champ_lcs]
        if cheaper:
            a = min(cheaper, key=lambda a: a.get("cone_lcs", 10**9))
            tags = [t for t in failure_tags(a.get("reason", "")) if t != "no-savings"]
            lim, detail = (tags[0] if tags else "menu"), f"{a['candidate']} (cone LC {a.get('cone_lcs')}) rejected: {a.get('reason', '')[:110]}"
        elif cheaper_in_bound:
            lim, detail = "menu", "cheaper candidate(s) inside the bound exist but none was attempted"
        else:
            over = sorted((e["error"], e["name"]) for e in entries if not e["within_bound"])
            if over and tour.get("bound"):
                lim = "cone-bound"
                detail = f"nearest over-bound candidate {over[0][1]}: error {over[0][0]:g} vs bound {tour['bound']:g}"
            else:
                lim, detail = "menu", "every candidate inside the bound was tried; none is cheaper"
    else:
        within = [e for e in entries if e["within_bound"]]
        if rejected and within:
            a = min(rejected, key=lambda a: a.get("cone_lcs", 10**9))
            tags = [t for t in failure_tags(a.get("reason", "")) if t != "no-savings"]
            lim, detail = (tags[0] if tags else "menu"), f"best attempt {a['candidate']}: {a.get('reason', '')[:110]}"
        elif within:
            best = min(within, key=lambda e: e["lcs"] or 10**9)
            lim = "menu"
            detail = f"best candidate inside the bound {best['name']}: cone LC {best['lcs']} vs exact cone {base_lcs} (no saving)"
        elif entries:
            over = sorted((e["error"], e["name"]) for e in entries)
            lim = "cone-bound"
            detail = f"nearest candidate {over[0][1]}: error {over[0][0]:g} vs bound {tour.get('bound', 0):g}"
        else:
            lim, detail = "menu", r.get("fallback_reason") or "no candidate generated"
    return {"id": r["id"], "name": r["name"], "method": method, "lc_saved_pct": r["savings_pct"]["logic_cells"],
            "exact_lc": r["exact"]["logic_cells"], "approx_lc": r["approx"]["logic_cells"], "tier": r["certification"]["tier"],
            "tightest": tight, "usage": usage, "limiter": lim, "detail": detail}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", required=True, metavar="LABEL=BUILD_DIR")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for spec in args.run:
        label, _, bdir = spec.partition("=")
        rows = []
        for rj in sorted(Path(bdir).glob("*/result.json")):
            rows.append(diagnose(json.loads(rj.read_text())))
        out[label] = rows
    od = Path(args.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    (od / "headroom.json").write_text(json.dumps(out, indent=2, default=str))
    md = ["# Headroom diagnosis", "",
          "Which constraint stops each design from being approximated further. `tightest` is the accepted design's largest "
          "constraint ratio (value / limit; 1.0 = at the limit). `limiter` is defined in `scripts/headroom_diagnosis.py`.", ""]
    for label, rows in out.items():
        md += [f"## {label}", "", "| Design | Technique | LC saved | Tightest constraint | Limiter | Detail |", "|:--|:--|--:|:--|:--|:--|"]
        for d in rows:
            t = d["tightest"]
            tt = f"{t['metric']} {t['ratio']:.3f}" if t else "n/a"
            md.append(f"| {d['name']} | `{d['method']}` | {d['lc_saved_pct']:.1f}% | {tt} | **{d['limiter']}** | {d['detail']} |")
        md.append("")
    (od / "headroom.md").write_text("\n".join(md) + "\n")
    print(f"Wrote {od / 'headroom.md'} ({sum(len(v) for v in out.values())} rows)")


if __name__ == "__main__":
    main()
