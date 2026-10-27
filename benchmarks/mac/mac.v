/*
 * Module: mac_unit
 * Standard: Verilog-2001
 * Source: https://github.com/thatCodey/CNN-Hardware-Accelerator---verilog
 * Purpose: Performs acc = acc + (a * b) using signed fixed-point logic.
 *
 * Annotated for AxSYS Attribute-Driven Approximate Logic Synthesis:
 * - Control signals ('enable', 'clear') protected with (* axc_precise = "1" *)
 * - Accumulator output ('acc_out') annotated with (* axc_relax = "1" *)
 */

module mac_unit (
    input                            clk,      // Clock
    input                            rst,      // Asynchronous Reset (Active High)
    (* axc_precise = "1" *) input    enable,   // Enable MAC operation
    (* axc_precise = "1" *) input    clear,    // Synchronous Clear for accumulator
    input  signed [7:0]              a,        // Multiplicand (8-bit signed)
    input  signed [7:0]              b,        // Multiplier   (8-bit signed)
    (* axc_relax = "1", axc_metric = "MED", axc_bound = "64.0", axc_lsb_width = 8 *)
    output reg signed [23:0]         acc_out   // Accumulator  (24-bit signed)
);

    // Internal wire for the product
    // 8-bit signed * 8-bit signed results in a 15-bit value + sign bit (16 bits)
    wire signed [15:0] product;
    assign product = a * b;

    // Sequential Logic
    always @(posedge clk or posedge rst) begin
        if (rst) begin
            acc_out <= 24'd0;
        end else begin
            if (clear) begin
                acc_out <= 24'd0;
            end else if (enable) begin
                /* * FIXED-POINT SAFETY:
                 * Adding a 16-bit signed product to a 24-bit accumulator.
                 * The sign-extension is handled automatically by the 'signed' 
                 * reg/wire types. 24 bits provides 8 bits of "guard" overhead,
                 * preventing overflow for up to 256 consecutive MAC cycles.
                 */
                acc_out <= acc_out + product;
            end
            // Implicit else: acc_out <= acc_out (hold value)
        end
    end

endmodule
