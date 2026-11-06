#!/usr/bin/env python3
"""REPORT.md + figures -> one self-contained REPORT.html (images embedded). Uses the system python `markdown` package."""
import base64
import re
import sys
from pathlib import Path

import markdown

CSS = """
body{font-family:'Helvetica Neue',Arial,sans-serif;color:#1f2933;max-width:980px;margin:32px auto;padding:0 24px;line-height:1.5;font-size:14px}
h1{font-size:28px;margin-bottom:4px} h2{font-size:20px;margin-top:40px;border-bottom:2px solid #1f6feb;padding-bottom:4px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:12px} th{background:#eef3fb;text-align:right;padding:6px 8px;border-bottom:2px solid #1f6feb}
th:first-child{text-align:left} td{padding:5px 8px;border-bottom:1px solid #e3e7ec} tr:nth-child(even) td{background:#f8fafc}
img{max-width:100%;display:block;margin:14px auto} code{background:#eef1f5;padding:1px 4px;border-radius:3px}
@media print{body{margin:0;max-width:none} h2{page-break-before:auto} img{page-break-inside:avoid} tr{page-break-inside:avoid}}
"""

def main(run: str) -> None:
    run = Path(run)
    md = (run / "REPORT.md").read_text()
    html = markdown.markdown(md, extensions=["tables"])
    def embed(m):
        data = base64.b64encode((run / m.group(1)).read_bytes()).decode()
        return f'src="data:image/png;base64,{data}"'
    html = re.sub(r'src="(figures/[^"]+)"', embed, html)
    (run / "REPORT.html").write_text(f"<!doctype html><meta charset='utf-8'><title>AxFPGA results</title><style>{CSS}</style>{html}")
    print("wrote", run / "REPORT.html")

main(sys.argv[1])
