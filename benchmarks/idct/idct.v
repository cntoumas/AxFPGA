`timescale 1ns/1ps

module idct (
    input  wire        clk,
    input  wire        rst_n,
    (* axc_precise = "1" *) input  wire valid_in,
    (* axc_precise = "1" *) output reg  valid_out,
    input  wire signed [7:0] X0,
    input  wire signed [7:0] X1,
    input  wire signed [7:0] X2,
    input  wire signed [7:0] X3,
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "16.0", axc_lsb_width = 4 *)
    output reg  signed [15:0] y0
);
    wire signed [15:0] c0 = X0 * 8'sd64;
    wire signed [15:0] c1 = X1 * 8'sd83;
    wire signed [15:0] c2 = X2 * 8'sd64;
    wire signed [15:0] c3 = X3 * 8'sd36;
    wire signed [17:0] sum = c0 + c1 + c2 + c3;
    wire signed [15:0] comb = sum[17:2];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            y0        <= 16'sd0;
        end else begin
            valid_out <= valid_in;
            if (valid_in) begin
                y0 <= comb;
            end
        end
    end
endmodule
