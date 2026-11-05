// image_pipeline: streaming edge-detection front end (RGB -> luma -> 3x3 Gaussian blur -> Sobel magnitude).
//
// Phase 4 benchmark (AxFPGA). Written for this repository; no third-party code.
//
// One RGB pixel enters per valid cycle in raster order (image width WIDTH); one edge pixel leaves per valid cycle.
//   Stage 1  luma      Y = (77 R + 150 G + 29 B) >> 8                      (ITU-R BT.601 weights, scaled by 256)
//   Stage 2  blur      B = (1 2 1; 2 4 2; 1 2 1) * Y >> 4                    (two line buffers + 3x3 window)
//   Stage 3  Sobel     E = min(255, (|Gx| + |Gy|) >> 2) on the blurred image (two line buffers + 3x3 window)
// Line buffers are shift registers of WIDTH pixels. No border handling: windows at the left/right/top border
// mix pixels of neighbouring rows, identically in the exact and the approximate design.
// Latency: about 2 * (2 * WIDTH + 2) valid cycles. All registers advance only when in_valid is high and have a
// synchronous active-high reset, so the design has a defined state after reset.
`timescale 1ns/1ps
module image_pipeline #(
    parameter WIDTH = 64                   // image width in pixels (line-buffer length)
) (
    input clk,
    input rst,
    input in_valid,
    input [23:0] rgb_in,
    output out_valid,
    output [7:0] edge_out
);
    // Ports are plain 'input' / 'output' lines (no wire/reg/signed keyword): the suite's attribute
    // annotator (pipeline.annotate_verilog) matches that form.
    wire en = in_valid;

    // ---------------- Stage 1: luma ----------------
    wire [7:0] r = rgb_in[23:16];
    wire [7:0] g = rgb_in[15:8];
    wire [7:0] b = rgb_in[7:0];
    wire [15:0] y_sum = 8'd77 * r + 8'd150 * g + 8'd29 * b;     // <= 256 * 255
    reg  [7:0] luma;
    always @(posedge clk) begin
        if (rst)
            luma <= 8'd0;
        else if (en)
            luma <= y_sum[15:8];
    end

    // ---------------- Stage 2: 3x3 Gaussian blur ----------------
    reg [8*WIDTH-1:0] lb1a, lb1b;          // luma one and two lines back
    reg [7:0] a00, a01, a02, a10, a11, a12, a20, a21, a22;   // window: row 0 oldest line, column 2 newest pixel
    wire [7:0] l1 = lb1a[8*WIDTH-1 -: 8];
    wire [7:0] l2 = lb1b[8*WIDTH-1 -: 8];
    always @(posedge clk) begin
        if (rst) begin
            lb1a <= {8*WIDTH{1'b0}};
            lb1b <= {8*WIDTH{1'b0}};
            {a00, a01, a02, a10, a11, a12, a20, a21, a22} <= 72'd0;
        end else if (en) begin
            lb1a <= {lb1a[8*WIDTH-9:0], luma};
            lb1b <= {lb1b[8*WIDTH-9:0], l1};
            a00 <= a01; a01 <= a02; a02 <= l2;
            a10 <= a11; a11 <= a12; a12 <= l1;
            a20 <= a21; a21 <= a22; a22 <= luma;
        end
    end
    wire [11:0] blur_sum = {4'd0, a00} + {3'd0, a01, 1'b0} + {4'd0, a02}
                         + {3'd0, a10, 1'b0} + {2'd0, a11, 2'b0} + {3'd0, a12, 1'b0}
                         + {4'd0, a20} + {3'd0, a21, 1'b0} + {4'd0, a22};          // <= 16 * 255
    reg [7:0] blur;
    always @(posedge clk) begin
        if (rst)
            blur <= 8'd0;
        else if (en)
            blur <= blur_sum[11:4];
    end

    // ---------------- Stage 3: Sobel magnitude ----------------
    reg [8*WIDTH-1:0] lb2a, lb2b;          // blurred image one and two lines back
    reg [7:0] s00, s01, s02, s10, s11, s12, s20, s21, s22;
    wire [7:0] m1 = lb2a[8*WIDTH-1 -: 8];
    wire [7:0] m2 = lb2b[8*WIDTH-1 -: 8];
    always @(posedge clk) begin
        if (rst) begin
            lb2a <= {8*WIDTH{1'b0}};
            lb2b <= {8*WIDTH{1'b0}};
            {s00, s01, s02, s10, s11, s12, s20, s21, s22} <= 72'd0;
        end else if (en) begin
            lb2a <= {lb2a[8*WIDTH-9:0], blur};
            lb2b <= {lb2b[8*WIDTH-9:0], m1};
            s00 <= s01; s01 <= s02; s02 <= m2;
            s10 <= s11; s11 <= s12; s12 <= m1;
            s20 <= s21; s21 <= s22; s22 <= blur;
        end
    end
    // Gx = right column - left column, Gy = bottom row - top row (weights 1 2 1); |G| <= 4 * 255.
    wire signed [11:0] gx = ({4'd0, s02} + {3'd0, s12, 1'b0} + {4'd0, s22})
                          - ({4'd0, s00} + {3'd0, s10, 1'b0} + {4'd0, s20});
    wire signed [11:0] gy = ({4'd0, s20} + {3'd0, s21, 1'b0} + {4'd0, s22})
                          - ({4'd0, s00} + {3'd0, s01, 1'b0} + {4'd0, s02});
    wire [10:0] ax = gx[11] ? -gx : gx;
    wire [10:0] ay = gy[11] ? -gy : gy;
    wire [11:0] mag = ax + ay;                                  // <= 2040
    wire [9:0]  mag4 = mag[11:2];
    wire [7:0]  edge_val = (mag4 > 10'd255) ? 8'd255 : mag4[7:0];

    reg [7:0] edge_r;
    reg       v;
    always @(posedge clk) begin
        if (rst) begin
            edge_r <= 8'd0;
            v <= 1'b0;
        end else begin
            if (en)
                edge_r <= edge_val;
            v <= en;
        end
    end

    assign edge_out = edge_r;
    assign out_valid = v;
endmodule
