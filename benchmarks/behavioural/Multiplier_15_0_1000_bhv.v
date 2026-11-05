// Behavioural drop-in for the AxBench array multiplier Multiplier_15_0_1000 (Aoki laboratory generator:
// two's complement 16x16, simple PPG, Wallace tree, Kogge-Stone final adder, one register stage).
//
// Same module name, ports and timing as the original, so the AxBench designs that instantiate it (multiplier.16b,
// neural.network, sobel.nn through qmult) use it unchanged:
//   - P is the low 32 bits of the two's complement product of IN1 and IN2;
//   - one register stage with synchronous active-high reset (the original registers its Wallace-tree outputs; after
//     reset both register halves are 0, so P is 0).
// The Verilog '*' lets synth_xilinx infer DSP48E1 blocks (DSP on) or a soft-logic multiplier (-nodsp), which is
// what the DSP-on/off comparison of the benchmark set needs. Added in Phase 4.
`timescale 1ns/1ps
module Multiplier_15_0_1000(P, IN1, IN2, clk, rst);
  input clk, rst;
  output [31:0] P;
  input [15:0] IN1;
  input [15:0] IN2;

  reg signed [31:0] P_reg;

  always @(posedge clk) begin
    if (rst)
      P_reg <= 32'sd0;
    else
      P_reg <= $signed(IN1) * $signed(IN2);
  end

  assign P = P_reg;
endmodule
