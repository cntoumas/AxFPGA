/*
 * Module: pe
 * Standard: IEEE 1800 SystemVerilog (INT8 Systolic Processing Element)
 * Source: https://github.com/ratshgupta-guptar76/int8-systolic-mac-array/blob/main/rtl/pe.sv
 * Author: Ratnesh Gupta (int8-systolic-mac-array)
 * 
 * Functionality:
 * Pipelined Multiply-Accumulate Processing Element for Systolic Tensor Arrays.
 * Supports weight/input data forwarding (a_out, b_out), active valid handshaking
 * (valid_out <= valid_in), synchronous accumulation clear, and signed INT8 arithmetic.
 *
 * Annotated for AxSYS Attribute-Driven Approximate Logic Synthesis:
 * - Control signals ('clear', 'valid_in', 'valid_out') protected with (* axc_precise = "1" *)
 * - Data forwarding path ('a_out', 'b_out') protected with (* axc_precise = "1" *)
 * - Datapath accumulator ('acc') relaxed with (* axc_relax = "1" *)
 */

`timescale 1ns/1ps

module pe #(
    parameter int DW = 8,
    parameter int K = 8
) (
    input logic clk,
    input logic rst_n,
    (* axc_precise = "1" *) input logic clear,

    input logic signed [DW-1:0] a_in,
    input logic signed [DW-1:0] b_in,
    (* axc_precise = "1" *) input logic valid_in,

    (* axc_precise = "1" *) output logic signed [DW-1:0] a_out,
    (* axc_precise = "1" *) output logic signed [DW-1:0] b_out,
    (* axc_precise = "1" *) output logic signed valid_out,
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "32.0", axc_lsb_width = 6 *)
    output logic signed [2*DW + 3 - 1:0] acc
);

    localparam int ACC_DW = 2*DW + 3;

    logic signed [ACC_DW-1:0] prod;
    
    assign prod = (a_in * b_in);
    
    always_ff @(posedge clk or negedge rst_n) begin
        if (~rst_n) begin
            a_out <= '0;
            b_out <= '0;
            acc   <= '0;
        end else begin
            if (valid_in) begin
                a_out <= a_in;
                b_out <= b_in;
                acc <= prod + acc;
            end else begin
                a_out <= a_out;
                b_out <= b_out;
                acc <= acc;
            end
            if (clear) begin
                acc <= '0;
            end
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (~rst_n) begin
            valid_out <= '0;
        end else begin
            valid_out <= valid_in;
        end
    end

endmodule
