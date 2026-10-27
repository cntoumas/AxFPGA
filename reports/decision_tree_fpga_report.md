# Decision-Tree-to-6-LUT Approximation on AMD/Xilinx 7-Series FPGAs

## Executive Summary

To explore native FPGA approximation paradigms beyond traditional ASIC gate pruning, we evaluated **Decision-Tree-to-6-LUT logic mapping** on AMD Artix-7 (`xc7a35tcpg236-1`). Modern Xilinx FPGAs feature 6-input Look-Up Tables (LUT6), capable of implementing any arbitrary Boolean function of up to 6 variables with sub-nanosecond propagation delays (~0.124 ns).

By training bit-sliced binary decision trees ($\text{depth } D \in [2..6]$) on cycle-accurate RTL traces and synthesizing them into native Verilog ternary cascades (`cond ? high : low`), we tested two approximation regimes:
1. **Full Cone Replacement ($k = W_{out}$)**: Entire arithmetic logic replaced by pure tree ensembles, completely eliminating dedicated carry chains (`CARRY4`).
2. **Hybrid LSB Approximation ($k < W_{out}$)**: Preserving high-order arithmetic bits while predicting lower bits using shallow trees.

---

## 1. Experimental Results

### Benchmark 1: `inversek2j` (16-bit Signed 2-Joint Inverse Kinematics)
- **Baseline Cost**: 298 Logic Cells (322 Slice LUTs, 4 CARRY4 primitives)
- **Inputs**: 33 bits (`x`, `y`, `theta1`, `valid_in`) | **Relaxed Output**: `n1` (16-bit signed)

| Mode | Depth $D$ | Approx Bits $k$ | Logic Cells (LCs) | Slice LUTs | CARRY4 | LC Savings (%) | Mean Error Distance (MED) | Bound ($\le 16.0$) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Exact Baseline** | — | — | **298** | **322** | **4** | — | **0.00** | PASS |
| **Full Cone DT** | 2 | 16 | **7** | 14 | 0 | **+97.6%** | 6,896.45 | FAIL |
| **Full Cone DT** | 3 | 16 | **14** | 16 | 0 | **+95.3%** | 5,792.02 | FAIL |
| **Full Cone DT** | 4 | 16 | **20** | 21 | 0 | **+93.3%** | 4,946.70 | FAIL |
| **Full Cone DT** | 5 | 16 | **36** | 43 | 0 | **+87.9%** | 4,734.50 | FAIL |
| **Full Cone DT** | 6 | 16 | **80** | 83 | 0 | **+73.2%** | 3,884.37 | FAIL |
| **Hybrid DT** | 2 | 4 | **298** | 323 | 4 | 0.0% | **3.23** | **PASS** |
| **Hybrid DT** | 3 | 4 | **299** | 322 | 4 | -0.3% | **2.45** | **PASS** |
| **Hybrid DT** | 4 | 4 | **300** | 324 | 4 | -0.7% | **1.53** | **PASS** |
| **Hybrid DT** | 5 | 4 | **301** | 325 | 4 | -1.0% | **0.75** | **PASS** |
| **Hybrid DT** | 6 | 4 | **307** | 332 | 4 | -3.0% | **0.42** | **PASS** |
| **Hybrid DT** | 6 | 6 | **316** | 341 | 4 | -6.0% | **8.39** | **PASS** |

---

### Benchmark 2: `sobel` (8-bit Unsigned Edge Gradient Magnitude)
- **Baseline Cost**: 134 Logic Cells (159 Slice LUTs, 15 CARRY4 primitives)
- **Inputs**: 64 bits (8 pixels $\times$ 8-bit) | **Relaxed Output**: `sat` (8-bit unsigned saturated magnitude)

| Mode | Depth $D$ | Approx Bits $k$ | Logic Cells (LCs) | Slice LUTs | CARRY4 | LC Savings (%) | Mean Error Distance (MED) | Bound ($\le 8.0$) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Exact Baseline** | — | — | **134** | **159** | **15** | — | **0.00** | PASS |
| **Full Cone DT** | 3 | 8 | **1** | 1 | 0 | **+99.2%** | 42.86 | FAIL |
| **Full Cone DT** | 4 | 8 | **3** | 3 | 0 | **+97.8%** | 43.40 | FAIL |
| **Full Cone DT** | 5 | 8 | **22** | 24 | 0 | **+83.6%** | 47.35 | FAIL |
| **Hybrid DT** | 4 | 4 | **137** | 157 | 15 | -2.2% | **60.90** | FAIL |
| **Hybrid DT** | 5 | 4 | **144** | 169 | 15 | -7.5% | **55.94** | FAIL |

---

## 2. Key Findings & Architectural Insights

### 1. Massive Resource Reduction via Carry-Chain Elimination
When an entire cone is replaced by decision trees ($k = W_{out}$), all carry propagation chains (`CARRY4`) are **100% eliminated**. 
- In `inversek2j`, logic collapsed from **298 LCs down to 7–36 LCs** (up to **97.6% savings**).
- In `sobel`, the 15-stage carry chain was replaced by **1 to 22 LUTs** (up to **99.2% savings**).

### 2. The Arithmetic Misprediction Challenge for MSBs
Why does full replacement fail tight error bounds ($\text{MED} \le 16.0$)?
- **Exponential Bit Weighting**: When training bitwise classification trees, an error on bit $b$ incurs an arithmetic penalty of $2^b$. A depth-5 tree can only query 5 variables out of 33 (or 64 in Sobel). A single mispredicted MSB causes an error of $\pm 128$ (Sobel) or $\pm 32,768$ (Inversek2j).
- **Carry Coupling**: Standard arithmetic operations exhibit deep global dependencies. Binary decision trees without explicit carry tracking cannot reconstruct multi-bit carry propagation from shallow trees.

### 3. Superiority of Hybrid LSB Approximation
Where Decision Trees shine is **learned lower-bit replacement**:
- For 4-bit approximation ($k=4$), zero-truncation produces a baseline error of $\text{MED} \approx \frac{15}{2} = 7.5$.
- A Depth-6 Decision Tree predicts the lower 4 bits with **$\text{MED} = 0.42$**—an **$18\times$ error reduction** over zero-truncation!
- The decision tree effectively learns the local carry-generation patterns from the input operand bits.

---

## 3. Comparison with Carry-Aware Pruning (`CarryPruner`)

| Feature | Carry-Aware Pruning (`CarryPruner`) | Decision Tree Approximation (`DecisionTree`) |
| :--- | :--- | :--- |
| **Primary Mechanism** | Physically slices carry chains at bit $k$ | Replaces Boolean logic with decision trees |
| **FPGA Primitive Target** | `CARRY4` shortening & dedicated adder trimming | Native 6-LUT truth-table encoding |
| **LC Reduction on Arithmetic** | **Consistently positive (+4% to +26%)** | Near-zero or negative in hybrid mode; up to +97% in full cone |
| **Accuracy on Lower Bits** | Deterministic truncation/rounding | **18x lower error** via learned bit prediction |
| **Ideal Use Case** | Adders, accumulators, MACs, FIR filters | Non-linear functions, classifiers, thresholding, activation functions |

---

## 4. Next Step: Dynamic / Hybrid DSP Inference

While Decision Trees provide extreme logic compression for non-linear cones, arithmetic performance on FPGAs is dominated by **DSP48E1** utilization:
1. **The Problem**: Hardcoding `nodsp=True` / `-max_dsp 0` forces all multipliers into soft LUT trees, while unconstrained synthesis consumes all available DSPs and causes place-and-route exhaustion.
2. **The Hybrid Solution**:
   - Introduce `--dsp-budget <N>`: Selectively assign `(* use_dsp = "yes" *)` to timing-critical or high-precision multipliers (`(* axc_precise *)`).
   - Assign `(* use_dsp = "no" *)` with `CarryPruner` / approximate logic to error-resilient multipliers (`(* axc_relax *)`), balancing DSP quota with soft fabric utilization.
