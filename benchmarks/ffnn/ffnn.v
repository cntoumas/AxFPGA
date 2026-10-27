`timescale 1ns/1ps

module ffnn (
    input  wire        clk,
    input  wire        rst_n,
    (* axc_precise = "1" *) input  wire valid_in,
    (* axc_precise = "1" *) output reg  valid_out,
    input  wire signed [7:0] in0,
    input  wire signed [7:0] in1,
    input  wire signed [7:0] in2,
    input  wire signed [7:0] in3,
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "8.0", axc_lsb_width = 4 *)
    output reg  [15:0] out_act
);
    wire signed [15:0] p0 = in0 * 8'sd24;
    wire signed [15:0] p1 = in1 * -8'sd18;
    wire signed [15:0] p2 = in2 * 8'sd35;
    wire signed [15:0] p3 = in3 * -8'sd10;
    wire signed [17:0] net = p0 + p1 + p2 + p3 + 18'sd128;
    wire [15:0] relu = (net[17]) ? 16'd0 : net[15:0];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            out_act   <= 16'd0;
        end else begin
            valid_out <= valid_in;
            if (valid_in) begin
                out_act <= relu;
            end
        end
    end
endmodule
