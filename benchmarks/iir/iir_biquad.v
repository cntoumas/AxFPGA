// iir_biquad: second-order IIR low-pass filter (direct form I), one sample per valid cycle.
//
// Phase 4 benchmark (AxFPGA), the feedback design for the sequential error bounds of Phase 6.
// Written for this repository; no third-party code.
//
//   y[n] = (B0 x[n] + B1 x[n-1] + B2 x[n-2] - A1 y[n-1] - A2 y[n-2]) >>> 14, saturated to 16 bits.
//
// Coefficients: 2nd-order Butterworth low-pass, cut-off 0.05 fs (scipy.signal.butter(2, 0.1)), rounded to Q2.14:
//   b = [0.020083, 0.040167, 0.020083] -> 329, 658, 329
//   a = [1, -1.561018, 0.641352]       -> 16384, -25576, 10508
// The quantized filter has DC gain exactly 1 (sum b = 1316 = 16384 - 25576 + 10508) and its poles have radius
// sqrt(10508 / 16384) = 0.80, so the loop is contracting: an error injected into y decays below 2^-16 of its size
// within 50 samples (accumulation horizon used by Phase 6: 64 samples, no clear period needed).
// With |x| <= 2^14 the output does not saturate (Butterworth: no gain above 1).
//
// Every register has a synchronous active-high reset; registers advance only when in_valid is high.
`timescale 1ns/1ps
module iir_biquad (
    input clk,
    input rst,
    input in_valid,
    input [15:0] x_in,
    output out_valid,
    output [15:0] y_out
);
    // Ports are plain 'input' / 'output' lines (no wire/reg/signed keyword): the suite's attribute
    // annotator (pipeline.annotate_verilog) matches that form.
    localparam signed [15:0] B0 = 16'sd329;
    localparam signed [15:0] B1 = 16'sd658;
    localparam signed [15:0] B2 = 16'sd329;
    localparam signed [15:0] A1 = -16'sd25576;
    localparam signed [15:0] A2 = 16'sd10508;

    wire signed [15:0] x0 = x_in;
    reg  signed [15:0] x1, x2;          // x[n-1], x[n-2]
    reg  signed [15:0] y1, y2;          // y[n-1], y[n-2]
    reg                v;

    wire signed [31:0] p0 = B0 * x0;
    wire signed [31:0] p1 = B1 * x1;
    wire signed [31:0] p2 = B2 * x2;
    wire signed [31:0] p3 = A1 * y1;
    wire signed [31:0] p4 = A2 * y2;
    wire signed [35:0] acc = p0 + p1 + p2 - p3 - p4;
    wire signed [21:0] ys = acc[35:14];                     // acc >>> 14 (floor)
    wire signed [15:0] y_new = (ys > 22'sd32767) ? 16'sd32767 :
                               (ys < -22'sd32768) ? -16'sd32768 : ys[15:0];

    always @(posedge clk) begin
        if (rst) begin
            x1 <= 16'sd0;
            x2 <= 16'sd0;
            y1 <= 16'sd0;
            y2 <= 16'sd0;
            v  <= 1'b0;
        end else begin
            if (in_valid) begin
                x1 <= x0;
                x2 <= x1;
                y1 <= y_new;
                y2 <= y1;
            end
            v <= in_valid;
        end
    end

    assign y_out = y1;
    assign out_valid = v;
endmodule
