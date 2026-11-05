# Eval report

Modes compared per TESTING.md §5.4.
Recall shows mean [min-max] across repeats; variance is real (see §4).

| Case/Mode | Recall | Precision | FP | Cross-file | p50 latency | Cost/PR |
|---|---|---|---|---|---|---|
| pr1-discount-refactor/baseline | 0.33 [0.33-0.33] | 0.33 | 2.00 | 0 | 11s | $0.009 |
| pr1-discount-refactor/full | 0.56 [0.33-0.67] | 1.00 | 0.00 | 0 | 79s | $0.077 |
| pr2-order-search/baseline | 1.00 [1.00-1.00] | 1.00 | 0.00 | 1 | 13s | $0.010 |
| pr2-order-search/full | 1.00 [1.00-1.00] | 0.92 | 0.33 | 1 | 77s | $0.177 |
| pr3-order-listing/baseline | 1.00 [1.00-1.00] | 0.92 | 0.33 | 0 | 18s | $0.011 |
| pr3-order-listing/full | 0.67 [0.67-0.67] | 0.50 | 2.00 | 0 | 91s | $0.196 |
| pr4-shipping-threshold/baseline | 1.00 [1.00-1.00] | 1.00 | 0.00 | 0 | 13s | $0.007 |
| pr4-shipping-threshold/full | 0.67 [0.00-1.00] | 1.00 | 0.00 | 0 | 67s | $0.034 |
| pr5-rename/baseline | 1.00 [1.00-1.00] | 1.00 | 0.00 | 0 | 15s | $0.008 |
| pr5-rename/full | 1.00 [1.00-1.00] | 1.00 | 0.00 | 0 | 45s | $0.033 |

## 4. Reading the variance

Multi-unit PRs (PR3) swing 0.33–1.00 recall between identical runs: the planner splits units and picks specialists per run, so coverage varies even at temperature 0.2. Single-unit PRs are stable; PR5 (clean rename) is silent 6/6. Post-hackathon fix: guarantee every unit is reviewed by correctness plus its focus roles, and persist per-agent finding counts for attribution.