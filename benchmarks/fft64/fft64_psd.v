// fft64_psd: streaming 64-point FFT power spectrum (radix-2 single-path delay feedback, decimation in frequency).
//
// Phase 4 benchmark (AxFPGA). Written for this repository; no third-party code.
//
// One complex sample (in_re, in_im: 16-bit two's complement) enters per valid cycle; one power-spectrum value
// leaves per valid cycle:   out_pow = re^2 + im^2  of bin X[k] / 64, bins in bit-reversed order.
//
// Six R2SDF stages with delay lengths 32, 16, 8, 4, 2, 1. Stage s:
//   - first half of each 2D-sample block: the input is written to the delay line and the delay-line output
//     (the differences of the previous block) leaves the stage, multiplied by the twiddle factor W64^(k * 2^s);
//   - second half: butterfly of the delay-line output a and the input b; (a + b) / 2 leaves the stage and
//     (a - b) / 2 enters the delay line.
// Each butterfly halves its result (total scaling 1/64), so no stage can overflow: |x| never grows.
// Twiddles W64^n = cos(2 pi n / 64) - j sin(2 pi n / 64) are Q1.14 constants (16384 = 1.0) in fft64_twiddle;
// the product is floored (>>> 14). With |in_re|, |in_im| <= 2^14 no internal value overflows 16 bits.
// Each stage registers its output; a stage's output is valid from the first second-half sample on.
// Every register has a synchronous active-high reset, so the design has a defined state after reset.
`timescale 1ns/1ps

module fft64_twiddle (
    input [4:0] n,
    output reg signed [15:0] c,      // round(16384 * cos(2 pi n / 64))
    output reg signed [15:0] s       // round(16384 * sin(2 pi n / 64))
);
    always @(*) begin
        case (n)
            5'd0:  begin c = 16'sd16384;  s = 16'sd0;     end
            5'd1:  begin c = 16'sd16305;  s = 16'sd1606;  end
            5'd2:  begin c = 16'sd16069;  s = 16'sd3196;  end
            5'd3:  begin c = 16'sd15679;  s = 16'sd4756;  end
            5'd4:  begin c = 16'sd15137;  s = 16'sd6270;  end
            5'd5:  begin c = 16'sd14449;  s = 16'sd7723;  end
            5'd6:  begin c = 16'sd13623;  s = 16'sd9102;  end
            5'd7:  begin c = 16'sd12665;  s = 16'sd10394; end
            5'd8:  begin c = 16'sd11585;  s = 16'sd11585; end
            5'd9:  begin c = 16'sd10394;  s = 16'sd12665; end
            5'd10: begin c = 16'sd9102;   s = 16'sd13623; end
            5'd11: begin c = 16'sd7723;   s = 16'sd14449; end
            5'd12: begin c = 16'sd6270;   s = 16'sd15137; end
            5'd13: begin c = 16'sd4756;   s = 16'sd15679; end
            5'd14: begin c = 16'sd3196;   s = 16'sd16069; end
            5'd15: begin c = 16'sd1606;   s = 16'sd16305; end
            5'd16: begin c = 16'sd0;      s = 16'sd16384; end
            5'd17: begin c = -16'sd1606;  s = 16'sd16305; end
            5'd18: begin c = -16'sd3196;  s = 16'sd16069; end
            5'd19: begin c = -16'sd4756;  s = 16'sd15679; end
            5'd20: begin c = -16'sd6270;  s = 16'sd15137; end
            5'd21: begin c = -16'sd7723;  s = 16'sd14449; end
            5'd22: begin c = -16'sd9102;  s = 16'sd13623; end
            5'd23: begin c = -16'sd10394; s = 16'sd12665; end
            5'd24: begin c = -16'sd11585; s = 16'sd11585; end
            5'd25: begin c = -16'sd12665; s = 16'sd10394; end
            5'd26: begin c = -16'sd13623; s = 16'sd9102;  end
            5'd27: begin c = -16'sd14449; s = 16'sd7723;  end
            5'd28: begin c = -16'sd15137; s = 16'sd6270;  end
            5'd29: begin c = -16'sd15679; s = 16'sd4756;  end
            5'd30: begin c = -16'sd16069; s = 16'sd3196;  end
            default: begin c = -16'sd16305; s = 16'sd1606; end   // n = 31
        endcase
    end
endmodule

module fft64_sdf_stage #(
    parameter LOGD = 5                       // delay length D = 2^LOGD; twiddle step 32 / D = 2^(5 - LOGD)
) (
    input clk,
    input rst,
    input in_valid,
    input signed [15:0] in_re,
    input signed [15:0] in_im,
    output reg out_valid,
    output reg signed [15:0] out_re,
    output reg signed [15:0] out_im
);
    localparam D = 1 << LOGD;

    reg [LOGD:0] cnt;                        // position in the 2D-sample block
    reg          started;                    // a second half has been seen: the output stream is valid
    wire         phase = cnt[LOGD];          // 0: first half, 1: second half

    // Delay line of D complex samples; the oldest sample is at the top.
    reg [16*D-1:0] dl_re, dl_im;
    wire signed [15:0] d_re = dl_re[16*D-1 -: 16];
    wire signed [15:0] d_im = dl_im[16*D-1 -: 16];

    // Butterfly, halved: (a + b) / 2 and (a - b) / 2 with a = delay-line output, b = input.
    wire signed [16:0] sum_re  = d_re + in_re;
    wire signed [16:0] sum_im  = d_im + in_im;
    wire signed [16:0] diff_re = d_re - in_re;
    wire signed [16:0] diff_im = d_im - in_im;
    wire signed [15:0] h_sum_re  = sum_re[16:1];
    wire signed [15:0] h_sum_im  = sum_im[16:1];
    wire signed [15:0] h_diff_re = diff_re[16:1];
    wire signed [15:0] h_diff_im = diff_im[16:1];

    wire signed [15:0] dl_in_re = phase ? h_diff_re : in_re;
    wire signed [15:0] dl_in_im = phase ? h_diff_im : in_im;
    wire signed [15:0] o_re = phase ? h_sum_re : d_re;
    wire signed [15:0] o_im = phase ? h_sum_im : d_im;
    wire [16*D+15:0] dl_re_next = {dl_re, dl_in_re};   // shift in at the bottom; the top 16 bits drop out
    wire [16*D+15:0] dl_im_next = {dl_im, dl_in_im};

    // Twiddle: first-half outputs are difference k of the previous block and get W64^(k * 2^(5 - LOGD));
    // second-half outputs (sums) get W64^0 = 1, which the floored Q1.14 product reproduces exactly.
    wire [5:0] cnt6 = cnt;                   // zero-extended block position
    wire [5:0] k = cnt6 & (D - 1);           // index within the half block
    wire [4:0] tw_n = phase ? 5'd0 : (k << (5 - LOGD));
    wire signed [15:0] tw_c, tw_s;
    fft64_twiddle tw (.n(tw_n), .c(tw_c), .s(tw_s));

    // (o_re + j o_im) * (c - j s) = (o_re c + o_im s) + j (o_im c - o_re s)
    wire signed [32:0] m_re = o_re * tw_c + o_im * tw_s;
    wire signed [32:0] m_im = o_im * tw_c - o_re * tw_s;
    wire signed [15:0] t_re = m_re[29:14];
    wire signed [15:0] t_im = m_im[29:14];

    always @(posedge clk) begin
        if (rst) begin
            cnt <= {(LOGD + 1){1'b0}};
            started <= 1'b0;
            dl_re <= {16*D{1'b0}};
            dl_im <= {16*D{1'b0}};
            out_valid <= 1'b0;
            out_re <= 16'sd0;
            out_im <= 16'sd0;
        end else begin
            out_valid <= in_valid & (phase | started);
            if (in_valid) begin
                cnt <= cnt + 1'b1;
                if (phase)
                    started <= 1'b1;
                dl_re <= dl_re_next[16*D-1:0];
                dl_im <= dl_im_next[16*D-1:0];
                out_re <= t_re;
                out_im <= t_im;
            end
        end
    end
endmodule

module fft64_psd (
    input clk,
    input rst,
    input in_valid,
    input [15:0] in_re,
    input [15:0] in_im,
    output out_valid,
    output [31:0] out_pow
);
    // Ports are plain 'input' / 'output' lines (no wire/reg/signed keyword): the suite's attribute
    // annotator (pipeline.annotate_verilog) matches that form.
    wire               v0, v1, v2, v3, v4, v5;
    wire signed [15:0] r0, r1, r2, r3, r4, r5;
    wire signed [15:0] i0, i1, i2, i3, i4, i5;

    fft64_sdf_stage #(.LOGD(5)) st0 (.clk(clk), .rst(rst), .in_valid(in_valid), .in_re(in_re), .in_im(in_im),
                                     .out_valid(v0), .out_re(r0), .out_im(i0));
    fft64_sdf_stage #(.LOGD(4)) st1 (.clk(clk), .rst(rst), .in_valid(v0), .in_re(r0), .in_im(i0),
                                     .out_valid(v1), .out_re(r1), .out_im(i1));
    fft64_sdf_stage #(.LOGD(3)) st2 (.clk(clk), .rst(rst), .in_valid(v1), .in_re(r1), .in_im(i1),
                                     .out_valid(v2), .out_re(r2), .out_im(i2));
    fft64_sdf_stage #(.LOGD(2)) st3 (.clk(clk), .rst(rst), .in_valid(v2), .in_re(r2), .in_im(i2),
                                     .out_valid(v3), .out_re(r3), .out_im(i3));
    fft64_sdf_stage #(.LOGD(1)) st4 (.clk(clk), .rst(rst), .in_valid(v3), .in_re(r3), .in_im(i3),
                                     .out_valid(v4), .out_re(r4), .out_im(i4));
    fft64_sdf_stage #(.LOGD(0)) st5 (.clk(clk), .rst(rst), .in_valid(v4), .in_re(r4), .in_im(i4),
                                     .out_valid(v5), .out_re(r5), .out_im(i5));

    // Power of each bin: |X|^2 <= 2 * (2^15)^2 = 2^31, unsigned 32 bits.
    wire [31:0] pow = r5 * r5 + i5 * i5;
    reg  [31:0] pow_r;
    reg         pv;
    always @(posedge clk) begin
        if (rst) begin
            pow_r <= 32'd0;
            pv <= 1'b0;
        end else begin
            pv <= v5;
            if (v5)
                pow_r <= pow;
        end
    end

    assign out_pow = pow_r;
    assign out_valid = pv;
endmodule
