/*
 * Module: rgb2ycbcr
 * Standard: IEEE 1364-2001 Verilog
 * Description: Pipelined Color Space Matrix Converter (RGB to ITU-R BT.601 YCbCr).
 *
 * Mathematical formulation (fixed point scaled by 256):
 *   Y  = ( 77*R + 150*G +  29*B ) >> 8
 *   Cb = (-43*R -  85*G + 128*B ) >> 8 + 128
 *   Cr = (128*R - 107*G -  21*B ) >> 8 + 128
 *
 * Annotated for AxSYS Attribute-Driven Approximate Logic Synthesis:
 * - Control signals ('valid_in', 'valid_out') protected with (* axc_precise = "1" *)
 * - Three independent multi-cone color datapaths (Y, Cb, Cr) annotated with (* axc_relax = "1" *)
 */

`timescale 1ns/1ps

module rgb2ycbcr (
    input  wire        clk,
    input  wire        rst_n,
    (* axc_precise = "1" *) input  wire valid_in,
    (* axc_precise = "1" *) output reg  valid_out,

    input  wire [7:0]  r_in,
    input  wire [7:0]  g_in,
    input  wire [7:0]  b_in,

    (* axc_relax = "1", axc_metric = "MED", axc_bound = "8.0", axc_lsb_width = 4 *)
    output reg  [7:0]  y_out,

    (* axc_relax = "1", axc_metric = "MED", axc_bound = "8.0", axc_lsb_width = 4 *)
    output reg  [7:0]  cb_out,

    (* axc_relax = "1", axc_metric = "MED", axc_bound = "8.0", axc_lsb_width = 4 *)
    output reg  [7:0]  cr_out
);

    // Color plane 1: Luma (Y) - Purely positive 16-bit summation
    wire [15:0] prod_yr = r_in * 8'd77;
    wire [15:0] prod_yg = g_in * 8'd150;
    wire [15:0] prod_yb = b_in * 8'd29;
    wire [16:0] sum_y   = {1'b0, prod_yr} + {1'b0, prod_yg} + {1'b0, prod_yb};
    wire [7:0]  y_comb  = sum_y[15:8];

    // Color plane 2: Chroma Blue (Cb) - Signed 16-bit calculation with +128 offset
    wire signed [16:0] prod_cbr = $signed({1'b0, r_in}) * -17'sd43;
    wire signed [16:0] prod_cbg = $signed({1'b0, g_in}) * -17'sd85;
    wire signed [16:0] prod_cbb = $signed({1'b0, b_in}) *  17'sd128;
    wire signed [17:0] sum_cb   = prod_cbr + prod_cbg + prod_cbb;
    wire signed [9:0]  cb_shift = sum_cb[17:8] + 10'sd128;
    wire [7:0]  cb_comb = (cb_shift < 10'sd0) ? 8'd0 :
                          (cb_shift > 10'sd255) ? 8'd255 : cb_shift[7:0];

    // Color plane 3: Chroma Red (Cr) - Signed 16-bit calculation with +128 offset
    wire signed [16:0] prod_crr = $signed({1'b0, r_in}) *  17'sd128;
    wire signed [16:0] prod_crg = $signed({1'b0, g_in}) * -17'sd107;
    wire signed [16:0] prod_crb = $signed({1'b0, b_in}) * -17'sd21;
    wire signed [17:0] sum_cr   = prod_crr + prod_crg + prod_crb;
    wire signed [9:0]  cr_shift = sum_cr[17:8] + 10'sd128;
    wire [7:0]  cr_comb = (cr_shift < 10'sd0) ? 8'd0 :
                          (cr_shift > 10'sd255) ? 8'd255 : cr_shift[7:0];

    // Synchronous registered output stage
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            y_out     <= 8'd0;
            cb_out    <= 8'd0;
            cr_out    <= 8'd0;
        end else begin
            valid_out <= valid_in;
            if (valid_in) begin
                y_out  <= y_comb;
                cb_out <= cb_comb;
                cr_out <= cr_comb;
            end
        end
    end

endmodule
