# Proposal

## Why
The README has a benchmark results table with placeholders (`_`) for concurrency, throughput, p50, and p99 latencies.
Filling this table provides evidence of DoubleLedge's performance and concurrency guarantees under row-level locking and contention.

## What Changes
- Enhance `scripts/benchmarks/run.py` to test and report the 3 README scenarios:
  1. Spread across 10k accounts (or 1,000 for fast local runs, minimal contention)
  2. 100 accounts (moderate contention)
  3. Single hot account (maximum row-lock contention)
- Run the benchmark script inside the Docker container to gather real measurements.
- Populate the benchmark markdown table in `README.md` with measured numbers.

## Capabilities
`skip_specs`: developer tooling, benchmark harness, and documentation update only.

## Impact
- `scripts/benchmarks/run.py`
- `README.md`
- No schema, model, or API changes.
