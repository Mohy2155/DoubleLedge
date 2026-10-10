# Tasks

## 1. Benchmark Harness Update
- [x] 1.1 Update `scripts/benchmarks/run.py` to support and report the three scenarios:
  - Spread across 10k accounts
  - 100 accounts
  - Single hot account (worst case)
  Verify the script outputs a consolidated results summary with Concurrency, Throughput, p50, and p99.

## 2. Benchmark Execution
- [x] 2.1 Run the benchmark harness inside the Docker container stack.
  Verify execution completes with 0 errors and `check_ledger_integrity` confirms 0 balance drift.

## 3. Documentation Update
- [x] 3.1 Populate the benchmark table in `README.md` with the measured results.
  Verify table formatting and commit changes.
