# Tasks

## 1. Django Models

- [x] 1.1 Define Django models for `Tenant`, `Account`, `JournalEntry`, `TransactionLine`, `IdempotencyKey`, and `OutboxEvent` in `ledger/models.py`. Use `DecimalField(max_digits=19, decimal_places=4)` for amounts. Verify by running `python manage.py makemigrations ledger --dry-run` and ensuring it builds a correct schema matching the raw SQL (though we will fake this migration in production).
- [x] 1.2 Write a test `tests/test_models.py` verifying that Django can query the existing tables properly using the models (since the tables were created in the previous PR).

## 2. Idempotency Layer

- [x] 2.1 Implement `IdempotencyKey` insertion logic in a utility function or decorator using `ON CONFLICT DO NOTHING` to safely avoid `IntegrityError` transaction aborts. Verify with a test asserting that duplicates return the replayed response.
- [x] 2.2 Write a test `tests/test_idempotency.py` proving that firing the same key concurrently from many threads correctly resolves to exactly one entry without raising constraint violations in the application layer.

## 3. Posting Service

- [x] 3.1 Implement the atomic `post_entry` service in `ledger/services/posting.py`. It must set the local tenant ID (`SET LOCAL app.tenant_id = %s`) and the lock timeout (`SET LOCAL lock_timeout = '2s'`). Verify by executing it via a test.
- [x] 3.2 Implement deterministic row locking inside `post_entry`: sort the `account_ids` and call `Account.objects.select_for_update().filter(id__in=account_ids).order_by("id")`. Verify with a test `tests/test_posting.py` confirming balances update correctly.

## 4. Concurrency and Resilience

- [x] 4.1 Write a decorator/wrapper around `post_entry` that catches `django.db.utils.OperationalError` with pgcode `40P01` or `40001` and retries using exponential backoff.
- [x] 4.2 Write a heavy concurrency test `tests/test_deadlocks.py` where opposed-order transfers (A→B and B→A) are executed concurrently in threads. Verify that none surface to the client as 500 errors and the final balances sum perfectly.
