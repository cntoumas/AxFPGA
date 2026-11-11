"""Quality attributes of the suite designs (one dict per suite id; plain data, no imports)."""

ANNOTATIONS = {
    "brent_kung_16b": {"format": "u", "quality": "wce<=15; med<=7.5; mred<=1%; app>=30", "plugin": "arith.py", "seed": 2026},
    "brent_kung_32b": {"format": "u", "quality": "wce<=255; med<=127.5; mred<=1%; app>=30", "plugin": "arith.py", "seed": 2026},
    "kogge_stone_16b": {"format": "u", "quality": "wce<=15; med<=7.5; mred<=1%; app>=30", "plugin": "arith.py", "seed": 2026},
    "kogge_stone_32b": {"format": "u", "quality": "wce<=255; med<=127.5; mred<=1%; app>=30", "plugin": "arith.py", "seed": 2026},
    "multiplier_16b": {"format": "s32", "quality": "wce<=65536; med<=32768; mred<=1%; app>=25", "plugin": "arith.py", "seed": 2026},
    "multiplier_32b": {"format": "s64", "quality": "wce<=4294967296; med<=2147483648; mred<=1%; app>=25", "plugin": "arith.py", "seed": 2026},
    "fir_filter": {"format": "u", "quality": "wce<=3; med<=1.5; mred<=1%; app>=25", "plugin": "fir.py", "seed": 2026},
    "sobel_filter": {"format": "u", "quality": "wce<=16; med<=8; app>=30", "plugin": "sobel_filter.py", "seed": 2026},
    "kinematics_fwd": {"format": "sm32q15", "quality": "wce<=1638; med<=819; mred<=1%; app<=0.05", "plugin": "kinematics.py", "seed": 2026},
    "kinematics_inv": {"format": "sm32q15", "quality": "wce<=1638; med<=819; mred<=1%; app<=0.05", "plugin": "kinematics.py", "seed": 2026},
    "kmeans_clustering": {"format": "s9", "quality": "wce<=3; med<=1.5; app<=2", "plugin": "kmeans.py", "seed": 2026},
    "neural_network": {"format": "sm16q9", "quality": "wce<=26; med<=13; mred<=1%; app>=99", "plugin": "neural_network.py", "seed": 2026},
    "sobel_npu": {"format": "sm16q9", "quality": "wce<=26; med<=13; mred<=1%; app>=30", "plugin": "sobel_npu.py", "seed": 2026},
}

BEHAVIOURAL_VARIANTS = ("multiplier_16b", "multiplier_32b", "neural_network", "kinematics_fwd", "kinematics_inv",
                        "sobel_npu")
for _id in BEHAVIOURAL_VARIANTS:
    ANNOTATIONS[_id + "_bhv"] = dict(ANNOTATIONS[_id])

ANNOTATIONS.update({
    "conv2d_3x3": {"format": "u8", "quality": "wce<=8; med<=2; app>=35", "plugin": "conv2d_plugin.py", "seed": 2026},
    "fft64_psd": {"format": "u32", "quality": "wce<=16384; med<=2048; app>=40", "plugin": "fft64_plugin.py",
                  "seed": 2026, "horizon": 128},
    "image_pipeline": {"format": "u8", "quality": "wce<=16; med<=4; app>=30", "plugin": "image_pipeline_plugin.py",
                       "seed": 2026},
    "iir_biquad": {"format": "s16", "quality": "wce<=32; med<=8; app>=40", "plugin": "iir_plugin.py", "seed": 2026,
                   "horizon": 64},
})
