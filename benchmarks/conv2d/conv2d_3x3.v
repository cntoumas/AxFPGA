// conv2d_3x3: one output pixel per cycle of a 3x3 convolution layer over C input channels (CNN layer engine).
//
// Phase 4 benchmark (AxFPGA). Written for this repository; no third-party code.
//
//   win      C*9 unsigned 8-bit activations, packed channel-major then row-major:
//            win[(c*9 + r*3 + k)*8 +: 8] is pixel (row r, column k) of channel c.
//   weights  C*9 signed 8-bit weights, packed like win. Ports, not constants, so each tap is a real
//            9x8-bit signed multiplier (DSP48E1 with DSP inference on, soft logic with -nodsp).
//   act_out  ReLU(sum of the C*9 products) >> SHIFT, saturated to 0..255 (requantized activation).
//
// Two register stages: S1 registers the window and the weights, S2 registers the activation computed by one
// combinational multiply-accumulate tree with ReLU and saturation. in_valid -> out_valid is the control path.
// The MAC tree sits between two register stages on purpose: the AxFPGA cone extractor treats the data input of
// an unrelaxed register as precise, so arithmetic behind an internal pipeline register would not be approximable
// in the current flow.
// Every register has a synchronous active-high reset, so the design has a defined state after reset.
`timescale 1ns/1ps
module conv2d_3x3 #(
    parameter C = 3,                       // input channels
    parameter SHIFT = 6                    // requantization shift (fixed-point weights scaled by 2^SHIFT)
) (
    input clk,
    input rst,
    input in_valid,
    input [C*9*8-1:0] win,
    input [C*9*8-1:0] weights,
    output out_valid,
    output [7:0] act_out
);
    // Ports are plain 'input' / 'output' lines (no wire/reg/signed keyword): the suite's attribute
    // annotator (pipeline.annotate_verilog) matches that form.
    localparam N = C * 9;

    // S1: input registers.
    reg [N*8-1:0] win_r;
    reg [N*8-1:0] w_r;
    reg           v1;
    always @(posedge clk) begin
        if (rst) begin
            win_r <= {N*8{1'b0}};
            w_r   <= {N*8{1'b0}};
            v1    <= 1'b0;
        end else begin
            win_r <= win;
            w_r   <= weights;
            v1    <= in_valid;
        end
    end

    // Multiply-accumulate tree: |acc| <= N * 255 * 128 < N * 2^15, so 24 bits hold N <= 256.
    integer i;
    reg signed [23:0] acc;
    always @(*) begin
        acc = 24'sd0;
        for (i = 0; i < N; i = i + 1)
            acc = acc + $signed({1'b0, win_r[i*8 +: 8]}) * $signed(w_r[i*8 +: 8]);
    end

    // ReLU, requantize, saturate.
    wire signed [23:0] q = acc >>> SHIFT;
    wire [7:0] act = acc[23] ? 8'd0 : ((q > 24'sd255) ? 8'd255 : q[7:0]);

    // S2: output registers.
    reg [7:0] act_r;
    reg       v2;
    always @(posedge clk) begin
        if (rst) begin
            act_r <= 8'd0;
            v2    <= 1'b0;
        end else begin
            act_r <= act;
            v2    <= v1;
        end
    end

    assign act_out = act_r;
    assign out_valid = v2;
endmodule
