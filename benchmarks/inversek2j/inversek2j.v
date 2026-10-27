`timescale 1ns/1ps

module inversek2j (
    input  wire        clk,
    input  wire        rst_n,
    (* axc_precise = "1" *) input  wire valid_in,
    (* axc_precise = "1" *) output reg  valid_out,
    input  wire signed [7:0] x,
    input  wire signed [7:0] y,
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "16.0", axc_lsb_width = 4 *)
    output reg  signed [15:0] theta1
);
    wire signed [15:0] x2 = x * x;
    wire signed [15:0] y2 = y * y;
    wire signed [16:0] r2 = x2 + y2;
    wire signed [15:0] t1_comb = (x * 8'sd4) + (y * 8'sd4) + r2[15:0];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            theta1    <= 16'sd0;
        end else begin
            valid_out <= valid_in;
            if (valid_in) begin
                theta1 <= t1_comb;
            end
        end
    end
endmodule
