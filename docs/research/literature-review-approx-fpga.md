# Literature review: automated approximate computing for FPGA designs (positioning AxFPGA)

Compiled 2026-11-02 from primary sources: publisher and proceedings pages, arXiv, author PDFs and official repositories. Citations `[Rn]` point to the reference list in Section 8. "Unverified" means I could not confirm the claim from a primary source. Check those claims before you cite them.

---

## 1. Summary of conclusions

Every building block of AxFPGA has close prior art. The novelty is the combination, so the paper must be framed and evaluated as a combination. In detail:
- Attribute-driven relax/precise annotation of Verilog with a safe-to-approximate cone analysis is Axilog [R1, R2]. Axilog targets ASICs, leaves the choice of what to approximate to the designer, and does not appear to be released (unverified).
- FPGA-specific automatic selection of approximate operators driven by an application-level quality target, using NSGA-II with ML surrogates, is autoAx/Xel-FPGAs [R22, R24]. That tool is MIT-licensed but needs Vivado and works from a component library, with no formal guarantees.
- Yosys-based approximate logic synthesis (ALS) with formal WCE guarantees through approximation miters and SAT/SMT is MUSCAT [R15]. MUSCAT is open source, but it targets ASIC area and has no application-level metric.
- Catalog-based LUT/AIG rewriting for FPGAs is pyALS itself [R17, R18].
- Exploiting the dual-output LUT6_2 for approximation is QUADOL (DATE 2026) [R44]. QUADOL directly overlaps AxFPGA's "LUT6_2 compressor" engine.
- Formal sequential error analysis exists: ASLAN [R8] and the approximation miter with model checking of Chandrasekharan et al. [R54].

I found no single tool that combines all of the following: (a) plain Verilog attributes as the interface, (b) an entirely open-source flow from Yosys to estimation, (c) FPGA-aware cost, (d) several heterogeneous approximation engines, (e) an application-level quality gate, and (f) formal proofs of control-path equivalence plus a combinational WCE bound for every accepted candidate. That combination is defensible as a tool contribution.

The planned NSGA-II/AMOSA relaxation selection is not new as an algorithm (autoAx, Xel-FPGAs, AxOSyn, E-IDEA and pyALS all use multi-objective metaheuristics). Its novelty must come from the following:
- the search space: which signals to relax and their widths, inside a formally safe cone;
- formal certification of every point on the Pareto front;
- being open source end to end.

Expect three strong objections:
- Yosys `synth_xilinx` estimates are not Vivado post-place-and-route results.
- DSP48 mapping makes LUT-multiplier approximation moot on Zynq.
- The WCE proof is combinational only, so error that accumulates in sequential logic is not covered (cf. [R8, R54]).

---

## 2. Comparison table

Legend. Columns: Auto = what is automated; Input = input abstraction; Search = search method; Formal = formal guarantees; OSS = open-source availability. Values: ASIC/FPGA = target; "lib" = library of approximate components; "app-QoR" = an application-level quality metric drives the search.

| Work | Auto | Input | Target | Search | Formal | OSS | Venue / year |
|---|---|---|---|---|---|---|---|
| **AxFPGA (this work)** | Picks the cheapest candidate per relaxed cone from several engines. Planned: signal/width selection | Verilog + IEEE attributes (`axc_relax`, `axc_precise`) | FPGA (7-series) | Tournament (cheapest passing). Planned: NSGA-II/AMOSA | Yosys SAT: control equivalence by temporal induction + combinational WCE bound | Yes (Yosys 0.33 + iverilog) | – |
| Axilog [R1,R2] | Safe-to-approximate inference only. Designer picks what to relax | Verilog annotations (`relax`, `relax_local`, `restrict`, `restrict_global`, `bridge`) | ASIC (Synopsys DC, TSMC 45 nm / NanGate 45 nm) | None (approximation by timing relaxation or a probabilistic gate model) | "Safety" by static analysis only. No error bound | Not found (unverified) | DATE 2015; IEEE Micro 2015 |
| ABACUS [R4,R5] | Behavioral-level variant generation | Behavioral HDL → abstract synthesis tree | ASIC | Iterative stochastic greedy | No | Unverified | DATE 2014; TETC 2019 |
| SALSA [R6] | Approximate don't-cares from an error bound | Gate netlist | ASIC (SIS, Synopsys DC) | Constructive | Error bound encoded as a circuit constraint | Unverified | DAC 2012 |
| SASIMI [R7] | Signal substitution under a quality bound | Gate netlist | ASIC | Greedy, quality-constrained loop | No (ER/MED estimated) | Unverified | DATE 2013 |
| ASLAN [R8] | ALS of sequential circuits under a quality limit | Sequential netlist | ASIC | Iterative | Yes: formal verification of a "Sequential Quality Constraint Circuit" | Unverified | DATE 2014 |
| BLASYS [R10,R11] | Subcircuit approximation via Boolean matrix factorization + DSE over order | Verilog (Yosys/ABC/iverilog) | ASIC (NAND count / Liberty) | Greedy DSE over partitions | No (simulation; HD/MAE/ER/MRE) | Yes, BSD-3, `scale-lab/BLASYS` | DAC 2018; TCAD 2021 |
| ALSRAC [R12,R13] | Resubstitution with an approximate care set | BLIF (ABC) | ASIC (genlib) | Greedy iterative | No (simulation) | Yes, `SJTU-ECTL/ALSRAC` (license not stated) | DAC 2020; TCAD ext. |
| Meng/Qian/De Micheli [R14] | ALS under a maximum-error constraint | Netlist | ASIC | Simulation-guided + SAT-based selection | Yes, max-error guarantee | Not stated | TCAD 2026 (arXiv 2025) |
| MUSCAT [R15] | Which cutpoints to set to constants (max set) | Verilog → Yosys netlist | ASIC (NanGate 45 nm via ABC) | MUS enumeration (z3, MUST) | Yes: valid by construction, approximation miter in SMT | Yes, `git.uni-paderborn.de/muscat/muscat` | DATE 2022 |
| CIRCA [R16] | Modular search-based ALS framework | Netlist (details unverified) | ASIC/generic | Pluggable search | Pluggable quality assurance | Yes, Paderborn GitLab (license not stated) | Microelectron. Reliab. 2019 |
| pyALS [R17,R18] | Catalog-based approximate rewriting of LUT/AIG cuts | Verilog/VHDL/SV via Yosys | FPGA (k-LUT) and AIG metrics | AMOSA (pyAMOSA) | No (simulation; 12 metrics incl. AWCE, MED, MRED) | Yes, GPL-3.0, `SalvatoreBarone/pyALS` | TETC 2022; JSA 2024 |
| E-IDEA / IDEA [R19] | App-driven selection of approximate variants | C/C++ (clang-Chimera) | HW and SW | Evolutionary (Bellerophon) | No | Partly (unverified) | IEEE Access 2021 |
| EvoApprox8b / EvoApproxLib [R21] | Component library (CGP-evolved) | – (component library; C/Verilog/Python models) | ASIC (subset FPGA [R23]) | CGP (offline) | Error metrics exact (offline) | Yes, MIT, `ehw-fit/evoapproxlib` | DATE 2017 |
| autoAx [R22] | Picks a lib component for each operator; app-QoR | RTL accelerator + lib | ASIC and FPGA | ML surrogates + hill climber / NSGA-II | No | Yes, MIT, `ehw-fit/autoax` | DAC 2019 |
| ApproxFPGAs [R23] | Picks FPGA-Pareto subset of ASIC lib via ML | Component library | FPGA | ML estimation | No | Yes, MIT, `ehw-fit/approx-fpgas` | DAC 2020 |
| Xel-FPGAs [R24] | End-to-end operator selection for FPGA accelerators; app-QoR | "Simple RTL description" + lib | FPGA (Vivado) | ML surrogates + NSGA-II / hill climber, hierarchical | No | Yes, MIT, `ehw-fit/xel-fpgas` (needs Vivado + ABC) | ICCAD 2023 |
| SMApproxLib [R25] | FPGA approximate multipliers (library) | – | FPGA (LUT/carry) | Manual design | No | Paper says "open source". Repo not found | DAC 2018 |
| AppAxO [R26] | Application-specific LUT/carry operator configurations | Operator config + app | FPGA | ML + GA-based DSE (unverified details) | No | Not found | ACM TECS 2022 |
| AxOCS [R27] | Scales operator DSE from small to large bit-widths | Operator config | FPGA | Metaheuristic MOO seeded by supersampling | No | Not found | IEEE TCAS-I 2024 |
| AxOMaP [R28] | FPGA operators via mathematical programming | Operator config | FPGA | MILP/MaP | No | Not found | ACM TRETS 2024 |
| AxOSyn [R29] | Selection + synthesis of operators, operator- and app-level | Operator models | FPGA (Vivado 2022.1, PYNQ-Z1 Zynq-7000) | GA (DEAP), random/patterned sampling | No | Claimed open source. URL blinded in preprint | arXiv 2025 (under review, TRETS) |
| Vašíček & Sekanina [R42] | CGP approximation, LUT-aware | Gate netlist | FPGA (4 commercial tools + ABC) | CGP | BDD-based relaxed equivalence | Unverified | FPL 2016 |
| Vašíček [R43] | Approximate adders/multipliers for k-LUT FPGAs | Gate/LUT netlist | FPGA (LUT count + LUT depth) | Iterative search | WCE-driven (exact eval.; formal method unverified) | Netlists online (URL unverified) | DDECS 2021 |
| QUADOL / QUADOL+ [R44] | Approximately merges LUT pairs into dual-output LUTs (LUT6_2) | LUT netlist | FPGA | Maximum matching + binary search | Unverified | Not found | DATE 2026 (arXiv v2 withdrawn) |
| Schlögl & Keszöcze [R45]; Echavarria et al. [R46] | Remove wires/LUTs from a LUT network; probability-based DSE | LUT netlist | FPGA | Probabilistic error propagation | No | Unverified | it-IT 2025; DCAS 2022 |
| AxLS [R47] | Netlist pruning (InOuts, ProbPrun), decision-tree ALS | Verilog via Yosys + iverilog | ASIC (NanGate 15 nm) | Greedy | No | Yes, `ECASLab/AxLS` (license not stated) | – (repo) |
| AxHLS [R31] | Picks approx adders/multipliers in HLS; analytical models | C | Unverified (AxME resource models) | DSEwam | No | Claimed open source. URL unverified | ICCAD 2020 |
| Leipnitz & Nazar [R32,R33] | Multi-technique approximate HLS under resource/real-time constraints | C (HLS) | FPGA | Heuristic DSE (unverified details) | No | Unverified | TRETS 2023; TECS 2019 |
| Li et al. [R34]; Lee et al. [R35] | Joint precision + HLS (scheduling, voltage) | C/DFG | ASIC | Optimization (unverified details) | No | Unverified | DAC 2015; DATE 2017 |
| Shi et al. [R36] | ALS inside approximate HLS | CDFG + gate netlist | Unverified | Unverified | Error bound (type unverified) | Not stated | ICCAD 2026 |
| Constantinides et al. [R37] | Multiple word-length allocation (linear DSP) | DFG | FPGA/custom | MILP (optimal) + heuristic | Analytical noise model | Unverified | IEEE TCAD 2003 |
| MiniBit [R38] | Integer + fraction bit-widths | Feed-forward DFG | FPGA | Range analysis + adaptive SA | Guarantees over user input intervals | Unverified | IEEE TCAD 2006 |
| Kinsman & Nicolici [R39] | Bit-width allocation | Numerical program | HW accelerators | SMT | Yes (SMT-proven bounds) | Unverified | IEEE TCAD 2010 |
| TAFFO [R40]; POPiX 2.0 [R41] | Float→fixed precision tuning (annotations) | C/C++ (LLVM); HLS `ap_fixed` | SW (TAFFO); FPGA HLS (POPiX) | Analysis-driven | No (static range analysis) | TAFFO yes (license unverified) | SoftwareX 2022; IEEE OJCAS 2025 |
| Češka/Vašíček/Mrázek et al. [R48–R51] | CGP evolution of approx. arithmetic | Gate netlist | ASIC | CGP, verifiability-driven | Yes: SAT-based WCE/WCRE in the loop | Unverified | ICCAD 2017; CAV 2018; EUROCAST 2019; ASOC 2020 |

---

## 3. Per-work notes

**Axilog** [R1, R2]
- Verilog annotations `relax`, `relax_local`, `restrict`, `restrict_global` and `bridge` (IEEE Micro text, Table I).
- A "Safety Inference Analysis" automatically infers the safe-to-approximate gates from the annotations.
- Approximation is applied by the synthesis flow: (1) timing relaxation of safe paths with Synopsys DC/PrimeTimeVX; (2) a probabilistic gate error model on NanGate FreePDK 45 nm.
- Results: 1.54× energy and 1.82× area at 10% quality loss (Micro). The DATE paper reports 54% energy and 1.9× area.
- Its Verilog benchmarks (Brent-Kung, FIR, InverseK, Sobel, Wallace tree, …) are the hardware part of AxBench [R3].
- Relevance: AxFPGA's `axc_relax`/`axc_precise` scheme and cone extraction are a direct descendant. Cite Axilog prominently and state the differences:
  - IEEE attributes instead of new keywords, so any Verilog-2005 tool parses them;
  - an error bound and metric attached to each output;
  - an FPGA target;
  - real logic rewriting instead of timing or voltage relaxation;
  - formal checks.

**AxBench** [R3]
- D&T 2017. CPU, GPU and hardware (Verilog) suites. Original repo `bitbucket.org/act-lab/axbench` is cited by a third-party Gitee mirror. I could not verify that the Bitbucket repo still exists.

**ABACUS** [R4, R5]
- Behavioral-level ALS: builds an abstract synthesis tree and applies variant operators with a stochastic greedy search.
- Closest older work to "automatic selection of what to approximate at the RTL level", but for ASIC.

**SALSA / SASIMI** [R6, R7]
- SALSA derives approximate don't-cares from an error bound using a quality-constraint circuit and reuses standard synthesis (SIS, DC).
- SASIMI substitutes signals that are equal with high probability.
- Both are gate-level ASIC methods. No code found.

**ASLAN, MACACO** [R8, R9]
- ASLAN extends ALS to sequential circuits with a "Sequential Quality Constraint Circuit" plus formal verification. It is the most relevant prior art for AxFPGA's sequential (induction) checks.
- MACACO (ICCAD 2011, Most Influential Paper award 2021) introduced rigorous analysis of approximate circuit behaviour.

**BLASYS** [R10, R11]
- Boolean matrix factorization of subcircuit truth tables.
- Open source (BSD-3) and uses Yosys, ABC, iverilog and LSOracle, so its toolchain is close to AxFPGA's.
- ASIC area only. Error from simulation.

**ALSRAC and successors** [R12–R14]
- Resubstitution with an approximate care set (BLIF/ABC).
- Successor: a simulation-guided max-error ALS with SAT-based selection and a worst-case guarantee (TCAD 2026). It is a strong modern formal-WCE ALS baseline, but for ASIC.

**MUSCAT** [R15]
- Implementation, from the PDF: Yosys reads Verilog; custom Yosys passes insert cutpoints and build an approximation miter; the miter is exported to SMT-LIBv2 and solved with z3 or MUST.
- Area is measured by ABC mapping to NanGate 45 nm.
- Reported: up to 80% more area savings than AIG rewriting and EvoApproxLib.
- **The closest methodological neighbour of AxFPGA's formal side: Yosys + miter + solver.** The difference: AxFPGA proves WCE on candidates produced by other engines, adds control equivalence by induction, and targets FPGA cost.

**CIRCA** [R16, R64]
- Paderborn's modular framework (input, approximation, quality assurance and search stages) built for fair comparisons.
- Source at `git.uni-paderborn.de/circa/public/circa_v2`. Useful as an architectural comparison point for the "tool" framing.

**pyALS / pyALSlib / pyAMOSA** [R17, R18, R20]
- Catalog-based AIG rewriting; HDL via Yosys (and GHDL for VHDL); k-LUT cut enumeration.
- AMOSA search with 12 error metrics; GPL-3.0.
- The FPGA variant (JSA 2024, vol. 150, art. 103112) implements the rewriting in ABC and exploits the similarity between k-feasible cut enumeration and LUT mapping.
- AxFPGA uses pyALS as an engine. Present it as such, not as a contribution.

**E-IDEA** [R19]
- Application-driven multi-objective approximation of C/C++ via clang-Chimera source-to-source rewriting and the Bellerophon evolutionary engine.
- Relevant as the "single application-level quality target → Pareto front" precedent from the same group as pyALS.

**EvoApprox8b / EvoApproxLib** [R21]
- CGP-evolved 8–16-bit adders and multipliers with exact MAE/WCE/MRE/EP/MSE.
- MIT; Verilog, C and Python models.
- `approx-fpgas` [R23] is the FPGA-characterized subset (MIT).

**autoAx → ApproxFPGAs → Xel-FPGAs** [R22–R24]
- Pipeline: generate random configurations, learn ML estimators (e.g. random forest) for QoR and hardware cost, then run hill climbing or NSGA-II and evaluate the front exactly with Vivado.
- Xel-FPGAs claims up to 95% less exploration time than plain synthesis and place-and-route.
- **This is the main prior art for AxFPGA's planned contribution.** It automatically assigns approximate components to the operators of an RTL accelerator for an application-level QoR target on FPGA.
- Differences to claim:
  - AxFPGA searches over signals/cones and bit-widths, not only over library slots;
  - it formally certifies results;
  - it uses no proprietary tools (Xel-FPGAs needs Vivado).

**TU Dresden FPGA operators (Ullah, Sahoo, Kumar)** [R25–R30]
- SMApproxLib (DAC 2018) is called "the first open source library of FPGA-based approximate multipliers", but I found no repo.
- AppAxO (TECS 2022), AxOCS (TCAS-I 2024), AxOMaP (TRETS 2024) and AxOSyn (arXiv 2025): LUT/carry-chain-level configurable operators with GA/MaP DSE.
- AxOSyn evaluates on a Zynq-7000 (PYNQ-Z1) with Vivado 2022.1, the same device family as the Zybo Z7-10, so it is a natural comparison point for operator-level PPA.
- I found no standalone work titled "AxO". The group uses "AxO" as a generic term for approximate operators (unverified).

**FPGA-specific ALS** [R42–R46]
- Vašíček & Sekanina (FPL 2016): gate-level CGP savings carry over to LUTs. They used BDD relaxed equivalence checking.
- Vašíček (DDECS 2021): LUT-count- and LUT-depth-aware synthesis of approximate adders and multipliers under WCE.
- **QUADOL (DATE 2026)** approximately merges LUT pairs into dual-output LUTs and reports up to 18% fewer LUTs than prior FPGA ALS. It overlaps AxFPGA's LUT6_2 compressor engine, so you must cite and differentiate it.
- FAU ACoF project: Schlögl & Keszöcze 2025; Echavarria et al. 2022.

**AxLS** [R47]
- Open-source Python + Yosys + iverilog ALS framework (TEC Costa Rica). Toolchain nearly identical to AxFPGA's, but ASIC-only with simulation-based MED/MRED.

**Approximate HLS** [R31–R36]
- AxHLS: C input, an approximate-FU library, AxME analytical models and DSEwam.
- Leipnitz & Nazar: FPGA-targeted approximate HLS under constraints, TRETS 2023.
- Li et al. (DAC 2015): joint precision optimization + HLS. Lee et al. (DATE 2017): adds voltage scaling.
- Shi et al. (ICCAD 2026): ALS inside approximate HLS, 11% average cost reduction at equal error bound.
- These automate selection, but from C rather than from existing HDL. AxFPGA's niche is designs that already exist as RTL.

**Word-length and precision tuning** [R37–R41]
- Constantinides et al.: MILP-optimal multiple word-length allocation.
- MiniBit: range analysis + adaptive simulated annealing with accuracy guarantees over input intervals.
- Kinsman & Nicolici: SMT-based bit-width allocation, which is formal.
- TAFFO: LLVM precision tuning with annotations.
- POPiX 2.0: automated `ap_fixed` precision for FPGA HLS.
- **Reviewers will ask how relaxation selection differs from word-length optimization.** You need a word-length baseline (Section 6).

**LLM / ML-generated approximate circuits (2026)** [R60, R65, R66]
- Tomasovic & Sekanina (PPSN 2026) co-evolve LLM prompts and circuits for 8-bit multipliers that beat EvoApproxLib.
- GTAC (arXiv 2601.19906) and E-ALS (arXiv 2609.13276) appear as titles in search results. Details unverified.
- Mention them only as emerging directions.

**Surveys to cite for framing** [R55–R57]
- Scarabottolo et al., Proc. IEEE 2020 (ALS survey).
- Sekanina, evolutionary approximation survey (arXiv 2108.07000).
- Leon et al., AxC survey part I (arXiv 2307.11124).

**Engines' origins**
- Lower-part-OR adder (LOA): Mahdiani et al., TCAS-I 2010 [R58]. Widely attributed; I did not confirm "LOA" in the abstract.
- Partial-product perforation: Zervakis et al., TVLSI 2016 [R59].
- Gate-level pruning: Schlachter et al., TVLSI 2017 [R63].
- AxFPGA's PPG truncation and LOA engines are standard techniques. Cite them; do not claim them.

---

## 4. Formal verification in approximate computing

| Approach | What is proved | Engine | Seq.? | Work |
|---|---|---|---|---|
| Error-bound circuit constraint (quality constraint circuit) | Error ≤ bound under don't-care optimization | Synthesis / ATPG-style | No | SALSA [R6] |
| Sequential quality constraint circuit | Quality bound across cycles | Formal verification | **Yes** | ASLAN [R8] |
| Approximation miter + model checking | Accumulated error over time (e.g. worst-case/accumulated) | Model checking | **Yes** | Chandrasekharan et al. DAC 2016 [R54] |
| Approximation-aware AIG rewriting | Guaranteed bounds for several metrics | BDD/SAT on AIG | No | Chandrasekharan et al. ICCAD 2016 [R53] |
| Exact error analysis | WCE, MSE, worst-case Hamming, WCRE | BDD vs SAT vs exhaustive, on ~2000 circuits up to 32 bit | No | Vašíček, IEEE Access 2019 [R52] |
| Verifiability-driven CGP | WCE/WCRE ≤ bound for each accepted candidate, with an adaptive SAT time limit | SAT inside evolution | No | Češka et al. ICCAD 2017, CAV 2018; Mrázek et al. ASOC 2020; Matyáš et al. EUROCAST 2019 [R48–R51] |
| Relaxed equivalence checking | Error bound of an FPGA-mapped approximation | BDD | No | Vašíček & Sekanina FPL 2016 [R42] |
| MUS over cutpoint constraints | Valid by construction (quality constraints, e.g. WCE) | SMT (z3, MUST) on a Yosys-built miter | No | MUSCAT [R15] |
| Max-error ALS | Worst-case error ≤ bound | SAT-based LAC selection | No | Meng et al. TCAD 2026 [R14] |
| Safety (non-interference) of approximation | Approximation only touches designer-selected elements | Static dataflow analysis | n/a | Axilog [R1, R2] |
| SMT bit-width bounds | Range and precision bounds | SMT | No | Kinsman & Nicolici [R39] |

**How AxFPGA compares.**
1. **Control-output equivalence by temporal induction** (Yosys `sat -tempinduct` style; see the Yosys docs [R62]). This is a formal version of what Axilog gives only through static analysis. Approximating the datapath must provably leave the precise outputs unchanged, including across sequential state. I found no prior approximate-computing tool that frames this as a per-candidate formal non-interference proof. That is a genuine, if modest, contribution. Precondition: the relaxed/precise cone split must be sound. Induction also has known incompleteness: it can fail without strengthening invariants. Report how often it is inconclusive.
2. **Combinational WCE bound by SAT on a miter.** This is the same technique as [R48–R52, R15]. It is not novel by itself. It is novel only as a *gate* applied uniformly to candidates from heterogeneous engines (pyALS, LOA, PPG truncation, LUT6_2) that themselves give no guarantee.
3. **Gaps relative to prior art:**
   - (a) No guarantee on **sequential error accumulation** for relaxed outputs that feed state, such as accumulators or IIR loops. ASLAN [R8] and [R54] handle this; AxFPGA's WCE proof is combinational.
   - (b) MED/MRED and application metrics (SQNR, PSNR, …) are **statistical**, not formal. Exact average-case analysis via BDDs exists [R52], at least for small widths.
   - (c) SAT-based WCE is known to struggle on wide multipliers. [R48, R50] needed adaptive time limits. Report proof times and time-outs.

---

## 5. Gap analysis and likely reviewer objections

**What is NOT novel**:
- Verilog-level relax/precise annotations and the cone analysis [R1, R2].
- Each approximation engine: pyALS [R17, R18], LOA [R58], perforation/truncation [R59], LUT6_2 merging [R44].
- SAT-based WCE checking [R48–R52, R15].
- Multi-objective DSE of approximate operators driven by application QoR on FPGA [R22, R24, R29].
- A Yosys + iverilog open-source ALS toolchain [R10, R15, R47].

**What is defensibly novel (to claim precisely)**:
1. The only flow I found that is (i) driven by attributes on unmodified Verilog, (ii) FPGA-cost-aware, (iii) fully free/open-source up to the estimate (Xel-FPGAs needs Vivado [R24]; AxOSyn uses Vivado [R29]; MUSCAT, BLASYS and AxLS are ASIC-only [R15, R11, R47]), and (iv) attaches a formal certificate to every accepted candidate.
2. Per-candidate formal **non-interference** of the precise control path, via induction. This turns Axilog's static "safety" into a checked property.
3. An engine-agnostic **tournament**: heterogeneous engines compete under one three-part acceptance gate (error budget + application metric + formal proof).
4. Planned: automatic selection of the relaxation set and bit-widths, inside the formally safe cone, against one application-level target, giving a **certified Pareto front**. Prior DSE (autoAx/Xel-FPGAs/AxOSyn) selects operator implementations, not which signals to relax. Every point on AxFPGA's front would also carry a proof. Check this claim against ABACUS [R4, R5] and word-length optimization [R37–R39], which do select precision per signal.

**Strongest reviewer objections (prepare answers and experiments)**:
1. *"Yosys estimates ≠ real FPGA results."* ApproxFPGAs [R23] showed that ASIC Pareto sets do not transfer to FPGA, and Vivado's packing, carry-chain inference and DSP mapping differ from `synth_xilinx`. Mitigation: validate the LUT/CARRY4/FF/DSP counts and power on a subset with Vivado place-and-route (and on the board), even though the flow itself stays open source. Report rank correlation between the two.
2. *"On Zynq, multipliers go into DSP48s; approximating LUT multipliers is pointless."* Mitigation: report results with DSPs both allowed and disallowed. Argue for DSP-starved or LUT-only regimes, or count DSPs as a scarce resource.
3. *"The LC = LUT + 2·CARRY4 cost is ad hoc."* Justify the weights, or report LUT, CARRY4 and power separately plus a sensitivity analysis.
4. *"The WCE proof is combinational; error in sequential designs can accumulate."* Cite [R8, R54]. Either add a bounded or inductive accumulated-error check, or state the limitation and restrict claims to feed-forward relaxed cones.
5. *"The engines are borrowed; where is the contribution?"* Frame the work as a framework/tool paper. Contributions: interface, safety, certification, open-source flow. Show ablations: each engine alone versus the tournament.
6. *"AxBench circuits are tiny and outdated."* Add at least one larger realistic kernel, such as a CNN layer, JPEG/DCT, or an FIR with many taps. Report scalability (cone size versus proof time).
7. *"Activity-based power estimates are unvalidated."* Compare against Vivado's `report_power` with SAIF from the same vectors, or against board measurements.
8. *"How is this different from word-length optimization?"* Run the truncation/word-length baseline at equal application quality (Section 6).
9. *"Why NSGA-II/AMOSA and not the Xel-FPGAs ML-surrogate approach?"* Compare against, or reuse, surrogates. Report the cost of formal checks inside the search loop: verify only candidates on the front, as in verifiability-driven search [R48, R50].
10. *"Novelty versus QUADOL for LUT6_2."* Cite [R44] and differentiate. AxFPGA builds compressors in a structured way, while QUADOL merges LUTs approximately and generically. Do not claim dual-output-LUT exploitation as novel.

---

## 6. Recommended baselines (with code availability)

| # | Baseline | Why | Code |
|---|---|---|---|
| B0 | Exact design, Yosys `synth_xilinx` and Vivado (DSP on/off) | Reference cost; checks the estimates | Yosys (ISC license, general knowledge); Vivado free WebPACK for xc7z010 (general knowledge) |
| B1 | **Uniform LSB truncation** of relaxed outputs/operands at equal application quality | Simplest competitor; reviewers will expect it | Trivial to implement in AxFPGA |
| B2 | **Per-signal word-length optimization** (MiniBit-style SA [R38] or greedy) at equal quality | Separates "relaxation selection" from classic WLO | Implement yourself. No public code found for [R37–R39] (unverified) |
| B3 | **pyALS alone** on the whole relaxed cone (AMOSA) | Shows what the tournament adds over its strongest engine | GPL-3.0, `github.com/SalvatoreBarone/pyALS` [R17] |
| B4 | **EvoApprox / approx-fpgas component substitution** (best single component per operator meeting the bound) | Standard library baseline | MIT, `github.com/ehw-fit/evoapproxlib`, `github.com/ehw-fit/approx-fpgas` [R21, R23] |
| B5 | **autoAx / Xel-FPGAs DSE** (NSGA-II + ML) on the same benchmarks (Sobel is included in the repo) | Closest prior art for the planned contribution | MIT, `github.com/ehw-fit/xel-fpgas` / `ehw-fit/autoax`. Needs Vivado + ABC [R22, R24] |
| B6 | **MUSCAT** under the same WCE bound | Formal-by-construction ALS on a Yosys flow | `git.uni-paderborn.de/muscat/muscat` [R15]; license unverified |
| B7 | BLASYS or ALSRAC, re-costed with `synth_xilinx` | Generic ALS baselines | BSD-3, `github.com/scale-lab/BLASYS` [R11]; `github.com/SJTU-ECTL/ALSRAC` [R12] (license not stated) |
| B8 | (Optional) AxOSyn / AppAxO operators | FPGA-native operators on the same Zynq-7000 family | Code not found (AxOSyn URL blinded [R29]) |
| B9 | (Optional) QUADOL for the LUT6_2 engine | Direct competitor | Code not found [R44] |

Minimal convincing set: **B0, B1, B2, B3, B4, B5**. Add B6 if you claim formal novelty over MUSCAT. Compare at **equal application-level quality** (iso-PSNR/SQNR), not at equal WCE.

---

## 7. Target venues with example papers

Conferences:
- **DAC**: autoAx 2019 [R22], ApproxFPGAs 2020 [R23], SMApproxLib 2018 [R25], ALSRAC 2020 [R12], BLASYS 2018 [R10], Chandrasekharan 2016 [R54].
- **DATE**: Axilog 2015 [R1], ABACUS 2014 [R4], ASLAN 2014 [R8], EvoApprox8b 2017 [R21], MUSCAT 2022 [R15], QUADOL 2026 [R44], Lee 2017 [R35].
- **ICCAD**: Xel-FPGAs 2023 [R24], AxHLS 2020 [R31], Češka 2017 [R48], Chandrasekharan 2016 [R53], Shi 2026 [R36].
- **FPL**: Vašíček & Sekanina 2016 [R42]. A strong fit for an FPGA tool paper.
- **FCCM, FPT, ISFPGA (FPGA symposium), ASP-DAC, GLSVLSI, DDECS** [R43]: suitable, but I did not collect AxC examples beyond DDECS.
- Open-source EDA workshops (good for an early tool release; venue details from general knowledge, check the current CFPs):
  - the free and open-source design automation workshop co-located with DATE (OSDA);
  - WOSET (co-located with ICCAD).

Journals:
- **ACM TRETS**: AxOMaP 2024 [R28], Leipnitz & Nazar 2023 [R32]. Best fit for FPGA tools; AxOSyn is under review there [R29].
- **IEEE TCAD**: Constantinides 2003 [R37], MiniBit 2006 [R38], Kinsman 2010 [R39], BLASYS 2021 [R11], Meng 2026 [R14].
- **IEEE TETC**: pyALS 2022 [R17], ABACUS 2019 [R5].
- **ACM TECS**: AppAxO 2022 [R26], Leipnitz 2019 [R33].
- **IEEE TCAS-I**: AxOCS 2024 [R27], Mahdiani 2010 [R58].
- **Journal of Systems Architecture**: pyALS-FPGA 2024 [R18].
- **IEEE Access**: E-IDEA 2021 [R19], Vašíček 2019 [R52].
- **Microelectronics Reliability**: CIRCA 2019 [R16].

Suggested path: tool paper with open-source artifact at **FPL or DATE**, then an extended journal version (with the NSGA-II/AMOSA contribution and Vivado validation) in **ACM TRETS or IEEE TCAD**.

---

## 8. References

- [R1] A. Yazdanbakhsh et al., "Axilog: Language Support for Approximate Hardware Design," DATE 2015. DOI 10.7873/DATE.2015.0513. https://past.date-conference.com/proceedings-archive/2015/html/0513.html
- [R2] A. Yazdanbakhsh et al., "Axilog: Abstractions for Approximate Hardware Design and Reuse," IEEE Micro 35(5):16–30, 2015. PDF: https://sites.cc.gatech.edu/grads/j/jpark632.deleted/doc/paper/2015-ieee_micro-axilog.pdf (DOI unverified)
- [R3] A. Yazdanbakhsh, D. Mahajan, H. Esmaeilzadeh, P. Lotfi-Kamran, "AxBench: A Multiplatform Benchmark Suite for Approximate Computing," IEEE Design & Test 34(2):60–68, 2017. https://cseweb.ucsd.edu/~hadi/doc/paper/2017-ieee_dt-axbench.pdf. Mirror: https://gitee.com/sheenisme/axbench (original bitbucket.org/act-lab/axbench unverified)
- [R4] K. Nepal, Y. Li, R. I. Bahar, S. Reda, "ABACUS: A Technique for Automated Behavioral Synthesis of Approximate Computing Circuits," DATE 2014. https://past.date-conference.com/date17/files/proceedings/2014/pdffiles/12.5_2.pdf (exact title wording unverified)
- [R5] K. Nepal, S. Hashemi, H. Tann, R. I. Bahar, S. Reda, "Automated High-Level Generation of Low-Power Approximate Computing Circuits," IEEE TETC, 2019 (DOI unverified)
- [R6] S. Venkataramani et al., "SALSA: Systematic Logic Synthesis of Approximate Circuits," DAC 2012. DOI 10.1145/2228360.2228504
- [R7] S. Venkataramani, K. Roy, A. Raghunathan, "Substitute-and-Simplify: A Unified Design Paradigm for Approximate and Quality Configurable Circuits," DATE 2013. https://past.date-conference.com/proceedings-archive/2013/PDFFILES/10.3_1.PDF
- [R8] A. Ranjan, A. Raha, S. Venkataramani, K. Roy, A. Raghunathan, "ASLAN: Synthesis of Approximate Sequential Circuits," DATE 2014. https://past.date-conference.com/date16/files/proceedings/2014/pdffiles/12.6_1.pdf
- [R9] R. Venkatesan, A. Agarwal, K. Roy, A. Raghunathan, "MACACO: Modeling and Analysis of Circuits for Approximate Computing," ICCAD 2011. https://engineering.purdue.edu/NRL/news/cbric-directors-alumni-win-iccad-influential-paper-award
- [R10] S. Hashemi, H. Tann, S. Reda, "BLASYS: Approximate Logic Synthesis Using Boolean Matrix Factorization," DAC 2018. DOI 10.1145/3195970.3196001. arXiv 1805.06050
- [R11] J. Ma, S. Hashemi, S. Reda, "Approximate Logic Synthesis Using Boolean Matrix Factorization," IEEE TCAD 41(1):15–28, 2021 (per repo). https://github.com/scale-lab/BLASYS (BSD-3-Clause)
- [R12] C. Meng, W. Qian, A. Mishchenko, "ALSRAC: Approximate Logic Synthesis by Resubstitution with Approximate Care Set," DAC 2020, 187:1–6. https://github.com/SJTU-ECTL/ALSRAC
- [R13] C. Meng, A. Mishchenko, W. Qian, G. De Micheli, "Efficient Resubstitution-Based Approximate Logic Synthesis," IEEE TCAD (2024 per repo, 2025 per other listing). https://github.com/changmg/ResubALS
- [R14] C. Meng, W. Qian, G. De Micheli, "Simulation-Guided Approximate Logic Synthesis Under the Maximum Error Constraint," IEEE TCAD 2026. arXiv 2505.16769
- [R15] L. Witschen, T. Wiersema, M. Artmann, M. Platzner, "MUSCAT: MUS-based Circuit Approximation Technique," DATE 2022. https://past.date-conference.com/proceedings-archive/2022/html/0748.html. Code: https://git.uni-paderborn.de/muscat/muscat
- [R16] L. Witschen, T. Wiersema, H. Ghasemzadeh Mohammadi, M. Awais, M. Platzner, "CIRCA: Towards a Modular and Extensible Framework for Approximate Circuit Generation," Microelectronics Reliability 99:277–290, 2019. DOI 10.1016/j.microrel.2019.04.003. Code: https://git.uni-paderborn.de/circa/public/circa_v2
- [R17] M. Barbareschi, S. Barone, N. Mazzocca, A. Moriconi, "A Catalog-based AIG-Rewriting Approach to the Design of Approximate Components," IEEE TETC, 2022. DOI 10.1109/TETC.2022.3170502. Code: https://github.com/SalvatoreBarone/pyALS (GPL-3.0); https://pypi.org/project/pyalslib/
- [R18] M. Barbareschi, S. Barone, N. Mazzocca, A. Moriconi, "FPGA approximate logic synthesis through catalog-based AIG-rewriting technique," J. Systems Architecture 150:103112, 2024. DOI 10.1016/j.sysarc.2024.103112
- [R19] S. Barone, M. Traiola, M. Barbareschi, A. Bosio, "Multi-Objective Application-Driven Approximate Design Method," IEEE Access, 2021. https://www.iris.unina.it/retrieve/12cf29ee-d1e5-481c-8279-1f0c61e342fe/Multi-Objective_Application-Driven_Approximate_Design_Method.pdf (DOI unverified)
- [R20] pyAMOSA, https://github.com/SalvatoreBarone/pyAMOSA (GPL-3.0). S. Bandyopadhyay, S. Saha, U. Maulik, K. Deb, "A Simulated Annealing-Based Multiobjective Optimization Algorithm: AMOSA," IEEE TEVC 12(3):269–283, 2008
- [R21] V. Mrazek, R. Hrbacek, Z. Vasicek, L. Sekanina, "EvoApprox8b: Library of Approximate Adders and Multipliers for Circuit Design and Benchmarking of Approximation Methods," DATE 2017, pp. 258–261. DOI 10.23919/DATE.2017.7926993. Code: https://github.com/ehw-fit/evoapproxlib (MIT)
- [R22] V. Mrazek, M. A. Hanif, Z. Vasicek, L. Sekanina, M. Shafique, "autoAx: An Automatic Design Space Exploration and Circuit Building Methodology utilizing Libraries of Approximate Components," DAC 2019. arXiv 1902.10807. Code: https://github.com/ehw-fit/autoax (MIT)
- [R23] B. S. Prabakaran, V. Mrazek, Z. Vasicek, L. Sekanina, M. Shafique, "ApproxFPGAs: Embracing ASIC-Based Approximate Arithmetic Components for FPGA-Based Systems," DAC 2020. DOI 10.1109/DAC18072.2020.9218533. arXiv 2004.10502. Code: https://github.com/ehw-fit/approx-fpgas (MIT)
- [R24] B. S. Prabakaran, V. Mrazek, Z. Vasicek, L. Sekanina, M. Shafique, "Xel-FPGAs: An End-to-End Automated Exploration Framework for Approximate Accelerators in FPGA-Based Systems," ICCAD 2023. DOI 10.1109/ICCAD57390.2023.10323678. arXiv 2303.04734. Code: https://github.com/ehw-fit/xel-fpgas (MIT)
- [R25] S. Ullah, S. S. Murthy, A. Kumar, "SMApproxLib: Library of FPGA-based Approximate Multipliers," DAC 2018. DOI 10.1145/3195970.3196115
- [R26] S. Ullah, S. S. Sahoo, N. Ahmed, D. Chaudhury, A. Kumar, "AppAxO: Designing Application-specific Approximate Operators for FPGA-based Embedded Systems," ACM TECS 21(3):29, 2022. https://cfaed.tu-dresden.de/files/Images/people/chair-pd/Papers/AppAxO.pdf
- [R27] S. S. Sahoo, S. Ullah, S. Bhattacharjee, A. Kumar, "AxOCS: Scaling FPGA-based Approximate Operators using Configuration Supersampling," IEEE TCAS-I 71(6), 2024. DOI 10.1109/TCSI.2024.3385333. arXiv 2309.12830
- [R28] S. S. Sahoo, S. Ullah, A. Kumar, "AxOMaP: Designing FPGA-based Approximate Arithmetic Operators using Mathematical Programming," ACM TRETS 17(2), 2024. DOI 10.1145/3648694 (as cited in [R29])
- [R29] S. S. Sahoo, S. Ullah, A. Kumar, "AxOSyn: An Open-source Framework for Synthesizing Novel Approximate Arithmetic Operators," arXiv 2507.20007, 2025
- [R30] S. Ullah, A. Kumar, "Approximate Arithmetic Circuit Architectures for FPGA-based Systems," Springer, 2023. ISBN 9783031212963
- [R31] J. Castro-Godínez, J. Mateus-Vargas, M. Shafique, J. Henkel, "AxHLS: Design Space Exploration and High-Level Synthesis of Approximate Accelerators using Approximate Functional Units and Analytical Models," ICCAD 2020. DOI 10.1145/3400302.3415732
- [R32] M. T. Leipnitz, G. L. Nazar, "Constraint-Aware Multi-Technique Approximate High-Level Synthesis for FPGAs," ACM TRETS 16(4):61, 2023
- [R33] M. T. Leipnitz, G. L. Nazar, "High-Level Synthesis of Approximate Designs under Real-Time Constraints," ACM TECS 18(5s):59, 2019
- [R34] C. Li, W. Luo, S. S. Sapatnekar, J. Hu, "Joint Precision Optimization and High Level Synthesis for Approximate Computing," DAC 2015, 104:1–6. DOI 10.1145/2744769.2744863
- [R35] S. Lee, L. K. John, A. Gerstlauer, "High-Level Synthesis of Approximate Hardware under Joint Precision and Voltage Scaling," DATE 2017. https://past.date-conference.com/proceedings-archive/2017/html/0150.html
- [R36] J. Shi, R. Dai, C. Meng, Y. Yang, W. Qian, "Integrating Approximate Logic Synthesis into Approximate High-Level Synthesis," ICCAD 2026. arXiv 2609.21697
- [R37] G. A. Constantinides, P. Y. K. Cheung, W. Luk, "Wordlength Optimization for Linear Digital Signal Processing," IEEE TCAD 22(10):1432–1442, 2003. DOI 10.1109/TCAD.2003.818119
- [R38] D.-U. Lee, A. A. Gaffar, R. C. C. Cheung, O. Mencer, W. Luk, G. A. Constantinides, "Accuracy-Guaranteed Bit-Width Optimization," IEEE TCAD 25(10):1990–2000, 2006. DOI 10.1109/TCAD.2006.873887
- [R39] A. B. Kinsman, N. Nicolici, "Bit-Width Allocation for Hardware Accelerators for Scientific Computing Using SAT-Modulo Theory," IEEE TCAD 29(3):405–413, 2010. DOI 10.1109/TCAD.2010.2041839
- [R40] D. Cattaneo et al., "TAFFO: The compiler-based precision tuner," SoftwareX, 2022. https://re.public.polimi.it/handle/11311/1223105 (author list and DOI unverified)
- [R41] Winandy et al., "Automated Fixed-Point Precision Optimization for FPGA Synthesis," IEEE Open J. Circuits and Systems, 2025 (POPiX 2.0). https://hal-lirmm.ccsd.cnrs.fr/FONISEN/hal-05564947v1 (details from a search snippet; page blocked, unverified)
- [R42] Z. Vašíček, L. Sekanina, "Search-based Synthesis of Approximate Circuits Implemented into FPGAs," FPL 2016, pp. 1–4. DOI 10.1109/FPL.2016.7577305
- [R43] Z. Vašíček, "Synthesis of Approximate Circuits for LUT-based FPGAs," DDECS 2021, pp. 17–22. DOI 10.1109/DDECS52668.2021.9417066
- [R44] J. Shi, X. Wang, C. Meng, W. Qian, "QUADOL: A Quality-Driven Approximate Logic Synthesis Method Exploiting Dual-Output LUTs for Modern FPGAs," DATE 2026. https://past.date-conference.com/proceedings-archive/2026/DATA/800.pdf. arXiv 2411.18330 (v2 withdrawn)
- [R45] T. Schlögl, O. Keszöcze, "Approximate synthesis for LUT count reduction via probabilistic error propagation," it – Information Technology, 2025. DOI 10.1515/itit-2024-0074
- [R46] J. A. Echavarria Gutiérrez, O. Keszöcze, J. Teich, "Probability-based DSE of Approximated LUT-based FPGA Designs," IEEE DCAS 2022. DOI 10.1109/dcas53974.2022.9845591
- [R47] J. Castro-Godínez, H. Barrantes-García, R. Morales-Monge, AxLS, https://github.com/ECASLab/AxLS (no paper citation given in repo; license not stated)
- [R48] M. Češka, J. Matyáš, V. Mrazek, L. Sekanina, Z. Vašíček, T. Vojnar, "Approximating Complex Arithmetic Circuits with Formal Error Guarantees: 32-bit Multipliers Accomplished," ICCAD 2017, pp. 416–423. DOI 10.1109/ICCAD.2017.8203807
- [R49] M. Češka et al., "ADAC: Automated Design of Approximate Circuits," CAV 2018 (pages and DOI unverified). Slides: https://human-competitive.org/sites/default/files/vasicek-slides.pdf
- [R50] M. Češka et al. (Mrazek, Vasicek, …), "Adaptive Verifiability-Driven Strategy for Evolutionary Approximation of Arithmetic Circuits," Applied Soft Computing, 2020. arXiv 2003.02491 (volume unverified)
- [R51] J. Matyáš, ..., M. Češka, "Approximating Complex Arithmetic Circuits with Guaranteed Worst-Case Relative Error," EUROCAST 2019 (pages unverified). https://gpbib.cs.ucl.ac.uk/gp-html/Ceska_2019_EUROCAST.html
- [R52] Z. Vašíček, "Formal Methods for Exact Analysis of Approximate Circuits," IEEE Access 7:177309–177331, 2019. DOI 10.1109/ACCESS.2019.2958605
- [R53] A. Chandrasekharan, M. Soeken, D. Große, R. Drechsler, "Approximation-aware Rewriting of AIGs for Error Tolerant Applications," ICCAD 2016. https://agra.informatik.uni-bremen.de/doc/konf/2016ICCAD_approximation_aware_rewriting.pdf
- [R54] A. Chandrasekharan, M. Soeken, D. Große, R. Drechsler, "Precise Error Determination of Approximated Components in Sequential Circuits with Model Checking," DAC 2016. DOI 10.1145/2897937.2898069
- [R55] I. Scarabottolo, G. Ansaloni, G. A. Constantinides, L. Pozzi, S. Reda, "Approximate Logic Synthesis: A Survey," Proc. IEEE 108(12):2195–2213, 2020. DOI 10.1109/JPROC.2020.3014430
- [R56] L. Sekanina, "Evolutionary Algorithms in Approximate Computing: A Survey," arXiv 2108.07000, 2021 (journal version unverified)
- [R57] V. Leon et al., "Approximate Computing Survey, Part I: Terminology and Software & Hardware Approximation Techniques," arXiv 2307.11124 (journal version unverified)
- [R58] H. R. Mahdiani, A. Ahmadi, S. M. Fakhraie, C. Lucas, "Bio-Inspired Imprecise Computational Blocks for Efficient VLSI Implementation of Soft-Computing Applications," IEEE TCAS-I 57(4):850–862, 2010 (author list per DBLP snippet; Ahmadi unverified)
- [R59] G. Zervakis, K. Tsoumanis, S. Xydis, D. Soudris, K. Pekmestzi, "Design-Efficient Approximate Multiplication Circuits Through Partial Product Perforation," IEEE TVLSI 24(10):3105–3117, 2016 (second author unverified; one source lists "Ntouskas")
- [R60] M. Tomasovic, L. Sekanina, "Multi-Objective Coevolution of Prompts and Templates for Circuit Approximation," PPSN 2026. arXiv 2606.13089
- [R62] Yosys documentation (the `sat` command incl. `-tempinduct`; `miter`), https://yosys.readthedocs.io/
- [R63] J. Schlachter, V. Camus, K. V. Palem, C. Enz, "Design and Applications of Approximate Circuits by Gate-Level Pruning," IEEE TVLSI 25(5):1694–1702, 2017. DOI 10.1109/TVLSI.2017.2657799
- [R64] L. Witschen, "Frameworks and Methodologies for Search-based Approximate Logic Synthesis," PhD thesis, Paderborn University, 2022. https://ris.uni-paderborn.de/record/34041
- [R65] "GTAC: A Generative Transformer for Approximate Circuits," arXiv 2601.19906 (authors and venue unverified)
- [R66] "E-ALS: Approximation-Potential-Aware E-Graph Rewriting for Approximate Logic Synthesis," arXiv 2609.13276 (authors and venue unverified)
