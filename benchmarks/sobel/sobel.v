`timescale 1ns/1ps

module sobel (
    input  wire        clk,
    input  wire        rst_n,
    (* axc_precise = "1" *) input  wire valid_in,
    (* axc_precise = "1" *) output reg  valid_out,
    input  wire [7:0]  p00, p01, p02,
    input  wire [7:0]  p10, p11, p12,
    input  wire [7:0]  p20, p21, p22,
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "8.0", axc_lsb_width = 4 *)
    output reg  [7:0]  edge_out
);
    wire signed [11:0] gx = ({3'b0, p02} + {2'b0, p12, 1'b0} + {3'b0, p22}) - ({3'b0, p00} + {2'b0, p10, 1'b0} + {3'b0, p20});
    wire signed [11:0] gy = ({3'b0, p20} + {2'b0, p21, 1'b0} + {3'b0, p22}) - ({3'b0, p00} + {2'b0, p01, 1'b0} + {3'b0, p02});
    wire [10:0] gx_abs = gx[11] ? -gx[10:0] : gx[10:0];
    wire [10:0] gy_abs = gy[11] ? -gy[10:0] : gy[10:0];
    wire [11:0] sum = {1'b0, gx_abs} + {1'b0, gy_abs};
    wire [7:0] sat = (sum > 12'd255) ? 8'hff : sum[7:0];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_out <= 1'b0;
            edge_out  <= 8'd0;
        end else begin
            valid_out <= valid_in;
            edge_out  <= sat;
        end
    end
endmodule
