"""RTL-level approximation operators (Phase 5): operand LSB truncation and output truncation, each with a free
constant correction, a genome over automatically enumerated sites, and greedy stacking."""

from .generator import OperatorGenerator, build_menu, neighbours, ranked_sites, run_operators  # noqa: F401
