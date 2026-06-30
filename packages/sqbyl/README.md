# sqbyl

The full text-to-SQL dev toolkit. See the [repository root README](../../README.md)
for the product overview, and `sqbyl-design-spec.md` /
`sqbyl-implementation-plan.md` for the design and build sequence.

This package contains the dev machinery — introspect, profile, annotate, synth,
the eval harness, the Coach, LLM judges, the review console, the orchestrator,
the optimizer, and the release builder. It depends on `sqbyl-runtime`
(never the reverse).
