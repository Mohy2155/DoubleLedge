# Design

## Context
See `proposal.md`. `scripts/benchmarks/run.py` currently tests a baseline scenario and a 2-account contention stress scenario. It does not map directly to the 3 distinct table scenarios documented in `README.md`:
1. `Spread across 10k accounts`
2. `100 accounts`
3. `Single hot account (worst case)`

## Goals / Non-Goals

**Goals:**
- Update `scripts/benchmarks/run.py` to run the 3 scenarios matching the README table.
- For local Docker environments, configure appropriate sample size and account pool (e.g. 10k accounts provisioned via bulk create, or scalable batches) so runs complete reliably in under 2 minutes.
- Output a clear summary table showing Concurrency, Throughput (tx/s), p50, and p99.
- Update `README.md` with the benchmark run results.

**Non-Goals:**
- External load testing tools (k6, Locust) or multi-node distributed benchmarks.
- Changing production posting logic or locking behavior.

## Decisions

- **Account setup strategy**:
  - Scenario 1 (Spread across 10k accounts): Bulk-insert accounts with `allow_negative=True` so transfers can pick random pairs with minimal lock conflicts. Concurrency = 16 or 32.
  - Scenario 2 (100 accounts): 100 accounts randomly paired. Moderate row-lock contention. Concurrency = 16.
  - Scenario 3 (Single hot account): All transfers involve a single central account (e.g. platform fee account or treasury). High contention demonstrating row-lock serialization. Concurrency = 16.
- **Metrics extraction**:
  - The script will calculate Throughput, p50 (median latency), p99 latency (99th percentile), and run `check_ledger_integrity` at the end to prove 0 balance drift.

## Risks / Trade-offs

- [Risk] Creating 10,000 accounts takes significant time during setup.
  → Mitigation: Use `Account.objects.bulk_create(batch_size=2000)` which creates 10,000 accounts in Postgres in < 2 seconds.
- [Risk] Lock timeouts under single hot account contention.
  → Mitigation: The posting engine already implements `retry_on_deadlock` and ascending account ID locking order to prevent deadlocks.
