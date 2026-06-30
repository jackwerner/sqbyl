# Instructions

This database tracks customer orders. Keep answers grounded in the semantic layer.

- "Revenue" means **net revenue**: confirmed orders only, refunds excluded. Prefer
  the `net_revenue` measure over ad-hoc arithmetic.
- Monetary columns are stored in **cents**; divide by 100 to report dollars.
- All timestamps are UTC. Relative windows ("last month") are relative to `now()`.
- Prefer the `monthly_recurring_revenue` trusted asset for any MRR question.
