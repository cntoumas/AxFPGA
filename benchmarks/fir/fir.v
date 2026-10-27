`timescale 1ns/1ps

module fir (
    input  wire        clk,
    input  wire        rst_n,
    (* axc_precise = "1" *) input  wire valid_in,
    (* axc_precise = "1" *) output reg  valid_out,
    input  wire signed [7:0] x_in,
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "16.0", axc_lsb_width = 5 *)
    output reg  signed [17:0] y_out
);
    reg signed [7:0] x0, x1, x2, x3;
    wire signed [15:0] p0 = x0 * 8'sd12;
    wire signed [15:0] p1 = x1 * 8'sd38;
    wire signed [15:0] p2 = x2 * 8'sd38;
    wire signed [15:0] p3 = x3 * 8'sd12;
    wire signed [17:0] sum = p0 + p1 + p2 + p3;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            x0 <= 8'sd0;
            x1 <= 8'sd0;
            x2 <= 8'sd0;
            x3 <= 8'sd0;
            y_out <= 18'sd0;
        end else begin
            valid_out <= valid_in;
            if (valid_in) begin
                x0 <= x_in;
                x1 <= x0;
                x2 <= x1;
                x3 <= x2;
                y_out <= sum;
            end
        end
    end
endmodule
