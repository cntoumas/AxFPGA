// Toy design annotated with attributes only (no Python plugin): 16-bit adder.
//   axc_relax    sum may be approximated
//   axc_format   sum is a 17-bit unsigned word
//   axc_quality  all must hold: worst-case error <= 15 (formal bound), mean error <= 8, PSNR >= 30 dB
//   axc_seed     seed of the random tuning stream; the held-out stream uses a derived seed
// Run: axfpga -i benchmarks/toy/toy_add.v -o build/toy
module toy_add (
    input  [15:0] a,
    input  [15:0] b,
    (* axc_relax, axc_format="u17", axc_quality="wce<=15;med<=8;psnr>=30", axc_seed=7 *)
    output [16:0] sum
);
    assign sum = a + b;
endmodule
