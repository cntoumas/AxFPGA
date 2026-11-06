"""Design-space exploration of the safe cone C_safe (metaheuristic search per approximation technique).

Modules:
  ops        per-site RTL operators on the word-level cone (pyosys, run in a subprocess)
  lutnet     LUT-level operators on the cone's 4-LUT mapping (per-LUT pyALS rewrites)
  evaluate   cone-level evaluation: error metrics on generated vectors, cost by Yosys synthesis, archive
  techniques one search space per technique (per-site genomes, or whole-cone candidate lists)
  search     mealpy runner, exhaustive enumeration, Pareto front, per-site explanation
"""
