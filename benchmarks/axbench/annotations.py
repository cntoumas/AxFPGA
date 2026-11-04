"""Quality attributes of the AxBench designs (one dict per suite id; plain data, no imports).

The suite copies these into the annotated private copy of each design, so AxBench runs through the same attribute
parser, plugin loader and evaluator as a user design. ``plugin`` names a file in this directory.
Budgets are in LSB units of the relaxed output in its number format (see docs/phases/phase-2-design.md, section 3).
"""

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
