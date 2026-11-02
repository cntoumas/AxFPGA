# Glossary

Domain terms used across AxFPGA. Code names follow these terms.

**Cone**: the safe combinational region extracted for approximation: logic that drives `(* axc_relax *)` signals and no `(* axc_precise *)` signal (`C_safe = C_relax \ C_precise`). Produced by `ConeExtractor` as `extracted_cone.v`.

**Cone Interface**: the module name, ports, bus ranges, relaxed output and its signedness of a Cone. Parsed once by `axfpga/cone_interface.py` so the Tournament and every Generator agree on the relaxed output.

**Bound**: the maximum error allowed on the relaxed output, from `axc_bound` (or `--bound`). A Candidate qualifies only if its measured error is at most the Bound.

**Metric**: the error measure the Bound applies to: `MED`, `WCE` or `ER`, from `axc_metric`.

**Candidate**: one approximate netlist of a Cone, proposed by a Generator.

**Generator**: a callable `(iface, work_dir, bound) -> list[candidate]` that proposes Candidates. `PyAlsBridge` is the production Generator.

**Tournament**: the module that simulates every Candidate against the exact Cone, scores qualifying ones in FPGA Logic Cells, and selects the Champion (`axfpga/tournament.py`).

**Champion**: the Candidate the Tournament selects: it meets the Bound and reduces Logic Cells or LUTs versus the exact Cone, ranked by (Logic Cells, LUTs, error).

**Fallback**: the Tournament's result when no Candidate both meets the Bound and saves resources: the exact Cone is returned and `Champion.fell_back` is true.

**Logic Cells (LC)**: the FPGA cost the Tournament minimises: `LUTs + 2 × CARRY4` as estimated from Yosys `synth_xilinx`.
