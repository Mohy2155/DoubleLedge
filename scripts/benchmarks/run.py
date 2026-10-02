import os
import sys
import uuid
import datetime
import time
import concurrent.futures
from decimal import Decimal
import statistics
import json
from collections import Counter

def setup_django():
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    
    from django.conf import settings
    if 'DEFAULT_THROTTLE_CLASSES' in settings.REST_FRAMEWORK:
        settings.REST_FRAMEWORK['DEFAULT_THROTTLE_CLASSES'] = []

setup_django()

from django.conf import settings
from rest_framework.test import APIClient
from ledger.models import Tenant, Account, JournalEntry
from django.db import connection
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

def generate_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem

def setup_data():
    private_pem, public_pem = generate_keys()
    settings.JWT_PUBLIC_KEY = public_pem

    tenant_name = f"Benchmark Tenant {uuid.uuid4().hex[:8]}"
    tenant = Tenant.objects.create(name=tenant_name)
    
    asset, _ = Account.objects.get_or_create(tenant=tenant, name="Asset", type=Account.Type.ASSET, currency="USD")
    asset.allow_negative = True
    asset.save()
    
    liability, _ = Account.objects.get_or_create(tenant=tenant, name="Liability", type=Account.Type.LIABILITY, currency="USD")
    liability.allow_negative = True
    liability.save()
    
    revenue, _ = Account.objects.get_or_create(tenant=tenant, name="Revenue", type=Account.Type.REVENUE, currency="USD")
    revenue.allow_negative = True
    revenue.save()
    
    expense, _ = Account.objects.get_or_create(tenant=tenant, name="Expense", type=Account.Type.EXPENSE, currency="USD")
    expense.allow_negative = True
    expense.save()

    # Seed initial balance properly via the ledger engine to maintain integrity
    from ledger.services.posting import post_entry
    import hashlib
    seed_payload = {
        "reference_id": f"SEED_{uuid.uuid4()}",
        "description": "Initial Seed",
        "lines": [
            {"account_id": str(asset.id), "amount": "1000000.0000", "direction": "DEBIT"},
            {"account_id": str(revenue.id), "amount": "1000000.0000", "direction": "CREDIT"}
        ]
    }
    idem_key = "seed_key"
    req_hash = hashlib.sha256(json.dumps(seed_payload, sort_keys=True).encode()).digest()
    post_entry(tenant=tenant, payload=seed_payload, idempotency_key=idem_key, request_hash=req_hash)
    
    payload = {
        "tenant_id": str(tenant.id),
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    }
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    
    return tenant, token, [asset, liability, revenue, expense]

def make_request(token, payload, idem_key):
    # Each thread needs its own db connection state, so close it at start just in case
    connection.close()
    
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer ' + token)
    
    start_time = time.perf_counter()
    response = client.post(
        '/api/v1/journal-entries/',
        data=payload,
        format='json',
        HTTP_IDEMPOTENCY_KEY=idem_key
    )
    elapsed = time.perf_counter() - start_time
    
    connection.close()
    return response.status_code, elapsed, response.data if response.status_code >= 400 else None

def run_load(name, token, requests_data, concurrency=10):
    print(f"\n--- Running Scenario: {name} ---")
    start_time = time.perf_counter()
    
    status_codes = Counter()
    latencies = []
    errors = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for req in requests_data:
            futures.append(executor.submit(make_request, token, req['payload'], req['idem_key']))
            
        for future in concurrent.futures.as_completed(futures):
            status, elapsed, err_data = future.result()
            status_codes[status] += 1
            latencies.append(elapsed)
            if status >= 500:
                errors.append(err_data)
                
    total_time = time.perf_counter() - start_time
    total_reqs = len(requests_data)
    tps = total_reqs / total_time
    
    print(f"Total Requests: {total_reqs}")
    print(f"Total Elapsed Time: {total_time:.2f}s")
    print(f"Throughput: {tps:.2f} req/sec")
    print("Status Codes:")
    for code, count in sorted(status_codes.items()):
        print(f"  {code}: {count}")
        
    if latencies:
        latencies.sort()
        p50 = latencies[int(len(latencies) * 0.5)]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"Latencies (ms): p50={p50*1000:.1f} | p95={p95*1000:.1f} | p99={p99*1000:.1f}")
        
    if status_codes.get(500, 0) > 0:
        print("CRITICAL: Encountered unhandled 500 errors!")
        for e in errors:
            print(e)
            
    return status_codes

def check_ledger_integrity(tenant, accounts):
    print("\n--- Checking Ledger Integrity ---")
    total_debits = Decimal('0.00')
    total_credits = Decimal('0.00')
    
    # Calculate expected balances from entries
    expected_balances = {str(a.id): Decimal('0.00') for a in accounts}
    type_map = {str(a.id): a.type for a in accounts}
    
    for entry in JournalEntry.objects.filter(tenant=tenant):
        for line in entry.lines.all():
            acc_type = type_map[str(line.account_id)]
            if line.direction == 'DEBIT':
                total_debits += line.amount
                if acc_type in ('ASSET', 'EXPENSE'):
                    expected_balances[str(line.account_id)] += line.amount
                else:
                    expected_balances[str(line.account_id)] -= line.amount
            else:
                total_credits += line.amount
                if acc_type in ('LIABILITY', 'EQUITY', 'REVENUE'):
                    expected_balances[str(line.account_id)] += line.amount
                else:
                    expected_balances[str(line.account_id)] -= line.amount
                
    assert total_debits == total_credits, f"Integrity failed: DEBITS {total_debits} != CREDITS {total_credits}"
    print(f"Double-entry Check Passed! Total Debits/Credits perfectly balanced at {total_debits}.")
    
    # Verify materialized balances
    for account in accounts:
        account.refresh_from_db()
        expected = expected_balances[str(account.id)]
        assert account.balance == expected, f"Account {account.name} balance {account.balance} != expected {expected}"
        
    print("Balance Materialization Check Passed! All accounts match sum of journal lines.")

def run_benchmark():
    tenant, token, accounts = setup_data()
    asset, liability, revenue, expense = accounts
    
    # 1. Baseline Throughput (100 sequential-ish unique transfers)
    baseline_reqs = []
    for i in range(100):
        baseline_reqs.append({
            'idem_key': f"baseline_{uuid.uuid4()}",
            'payload': {
                "reference_id": f"BASE_{uuid.uuid4()}",
                "description": "Baseline throughput test",
                "lines": [
                    {"account_id": str(asset.id), "amount": "10.0000", "direction": "CREDIT"},
                    {"account_id": str(expense.id), "amount": "10.0000", "direction": "DEBIT"}
                ]
            }
        })
        
    run_load("Baseline Throughput", token, baseline_reqs, concurrency=10)
    
    # 2. Contention / Hot-Spot Stress (100 concurrent transfers involving the same exact accounts)
    contention_reqs = []
    for i in range(100):
        # We mix directions randomly to cause massive row lock contention on just 2 accounts
        contention_reqs.append({
            'idem_key': f"contention_{uuid.uuid4()}",
            'payload': {
                "reference_id": f"CONT_{uuid.uuid4()}",
                "description": "Contention throughput test",
                "lines": [
                    {"account_id": str(asset.id), "amount": "1.0000", "direction": "CREDIT" if i % 2 == 0 else "DEBIT"},
                    {"account_id": str(revenue.id), "amount": "1.0000", "direction": "DEBIT" if i % 2 == 0 else "CREDIT"}
                ]
            }
        })
        
    status_codes = run_load("Contention Stress", token, contention_reqs, concurrency=20)
    
    assert status_codes.get(500, 0) == 0, "Failed: Encountered 500 errors (potential deadlocks)"
    
    check_ledger_integrity(tenant, accounts)
    
    print("\nBenchmark completed successfully. 0 Unhandled 500s. 0 Deadlocks.")

if __name__ == '__main__':
    run_benchmark()
