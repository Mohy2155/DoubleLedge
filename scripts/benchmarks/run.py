import os
import sys
import uuid
import datetime
import time
import concurrent.futures
from decimal import Decimal
import json
import random
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

def setup_tenant():
    private_pem, public_pem = generate_keys()
    settings.JWT_PUBLIC_KEY = public_pem

    tenant_name = f"Benchmark Tenant {uuid.uuid4().hex[:8]}"
    tenant = Tenant.objects.create(name=tenant_name)

    payload = {
        "tenant_id": str(tenant.id),
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
    }
    token = jwt.encode(payload, private_pem, algorithm="RS256")
    return tenant, token

def make_request(token, payload, idem_key):
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

def run_load(name, token, requests_data, concurrency=16):
    print(f"\n--- Running Scenario: {name} (Concurrency: {concurrency}, Requests: {len(requests_data)}) ---")
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
    
    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.5)] * 1000 if latencies else 0.0
    p95 = latencies[int(len(latencies) * 0.95)] * 1000 if latencies else 0.0
    p99 = latencies[int(len(latencies) * 0.99)] * 1000 if latencies else 0.0
    
    print(f"Total Requests: {total_reqs} | Elapsed: {total_time:.2f}s | Throughput: {tps:.1f} tx/s")
    print(f"Latencies (ms): p50={p50:.1f} | p95={p95:.1f} | p99={p99:.1f}")
    print(f"Status Codes: {dict(status_codes)}")
    
    if status_codes.get(500, 0) > 0:
        print("CRITICAL: Encountered unhandled 500 errors!")
        for e in errors[:3]:
            print(e)
            
    return {
        "name": name,
        "concurrency": concurrency,
        "throughput": round(tps, 1),
        "p50": round(p50, 1),
        "p99": round(p99, 1),
        "status_codes": status_codes
    }

def check_ledger_integrity(tenant, accounts):
    print("\n--- Checking Ledger Integrity ---")
    total_debits = Decimal('0.0000')
    total_credits = Decimal('0.0000')
    
    expected_balances = {str(a.id): Decimal('0.0000') for a in accounts}
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
    
    drift_count = 0
    for account in accounts:
        account.refresh_from_db()
        expected = expected_balances[str(account.id)]
        if account.balance != expected:
            drift_count += 1
            print(f"DRIFT: Account {account.name} balance {account.balance} != expected {expected}")
            
    assert drift_count == 0, f"Found {drift_count} accounts with balance drift!"
    print(f"Balance Materialization Check Passed! Checked {len(accounts)} accounts with 0 drift.")
    return 0

def run_benchmark():
    tenant, token = setup_tenant()
    
    print("\n=======================================================")
    print("DoubleLedge Concurrency & Throughput Benchmark Suite")
    print("=======================================================")
    
    results = []

    # -------------------------------------------------------------------------
    # Scenario 1: Spread across 10k accounts (minimal lock contention)
    # -------------------------------------------------------------------------
    print("\nProvisioning 10,000 accounts for Scenario 1...")
    accounts_10k = [
        Account(
            tenant=tenant,
            name=f"Acc_10k_{i}",
            type=Account.Type.ASSET if i % 2 == 0 else Account.Type.LIABILITY,
            currency="USD",
            allow_negative=True
        )
        for i in range(10000)
    ]
    Account.objects.bulk_create(accounts_10k, batch_size=2000)
    accounts_10k = list(Account.objects.filter(tenant=tenant, name__startswith="Acc_10k_"))
    
    reqs_10k = []
    num_requests = 1000
    for i in range(num_requests):
        idx1 = random.randint(0, 9999)
        idx2 = (idx1 + random.randint(1, 9999)) % 10000
        a1 = accounts_10k[idx1]
        a2 = accounts_10k[idx2]
        reqs_10k.append({
            'idem_key': f"s1_{uuid.uuid4()}",
            'payload': {
                "reference_id": f"S1_{uuid.uuid4()}",
                "description": "Spread across 10k accounts",
                "lines": [
                    {"account_id": str(a1.id), "amount": "10.0000", "direction": "CREDIT"},
                    {"account_id": str(a2.id), "amount": "10.0000", "direction": "DEBIT"}
                ]
            }
        })
    res_10k = run_load("Spread across 10k accounts", token, reqs_10k, concurrency=16)
    results.append(res_10k)

    # -------------------------------------------------------------------------
    # Scenario 2: 100 accounts (moderate contention)
    # -------------------------------------------------------------------------
    print("\nProvisioning 100 accounts for Scenario 2...")
    accounts_100 = [
        Account(
            tenant=tenant,
            name=f"Acc_100_{i}",
            type=Account.Type.ASSET if i % 2 == 0 else Account.Type.LIABILITY,
            currency="USD",
            allow_negative=True
        )
        for i in range(100)
    ]
    Account.objects.bulk_create(accounts_100)
    accounts_100 = list(Account.objects.filter(tenant=tenant, name__startswith="Acc_100_"))
    
    reqs_100 = []
    for i in range(500):
        idx1 = random.randint(0, 99)
        idx2 = (idx1 + random.randint(1, 99)) % 100
        a1 = accounts_100[idx1]
        a2 = accounts_100[idx2]
        reqs_100.append({
            'idem_key': f"s2_{uuid.uuid4()}",
            'payload': {
                "reference_id": f"S2_{uuid.uuid4()}",
                "description": "100 accounts contention",
                "lines": [
                    {"account_id": str(a1.id), "amount": "5.0000", "direction": "CREDIT"},
                    {"account_id": str(a2.id), "amount": "5.0000", "direction": "DEBIT"}
                ]
            }
        })
    res_100 = run_load("100 accounts", token, reqs_100, concurrency=16)
    results.append(res_100)

    # -------------------------------------------------------------------------
    # Scenario 3: Single hot account (maximum row-lock contention)
    # -------------------------------------------------------------------------
    print("\nProvisioning Single Hot Account & counter-parties for Scenario 3...")
    hot_account = Account.objects.create(
        tenant=tenant,
        name="Hot_Master_Account",
        type=Account.Type.LIABILITY,
        currency="USD",
        allow_negative=True
    )
    counter_parties = [
        Account(
            tenant=tenant,
            name=f"Counterparty_{i}",
            type=Account.Type.ASSET,
            currency="USD",
            allow_negative=True
        )
        for i in range(200)
    ]
    Account.objects.bulk_create(counter_parties)
    counter_parties = list(Account.objects.filter(tenant=tenant, name__startswith="Counterparty_"))
    
    reqs_hot = []
    for i in range(300):
        cp = counter_parties[i % len(counter_parties)]
        reqs_hot.append({
            'idem_key': f"s3_{uuid.uuid4()}",
            'payload': {
                "reference_id": f"S3_{uuid.uuid4()}",
                "description": "Hot account contention",
                "lines": [
                    {"account_id": str(hot_account.id), "amount": "1.0000", "direction": "CREDIT" if i % 2 == 0 else "DEBIT"},
                    {"account_id": str(cp.id), "amount": "1.0000", "direction": "DEBIT" if i % 2 == 0 else "CREDIT"}
                ]
            }
        })
    res_hot = run_load("Single hot account (worst case)", token, reqs_hot, concurrency=16)
    results.append(res_hot)

    # -------------------------------------------------------------------------
    # Integrity Verification
    # -------------------------------------------------------------------------
    all_accounts = accounts_10k + accounts_100 + [hot_account] + counter_parties
    drift = check_ledger_integrity(tenant, all_accounts)

    # -------------------------------------------------------------------------
    # Final Table Summary (README Markdown Format)
    # -------------------------------------------------------------------------
    print("\n=======================================================")
    print("FINAL BENCHMARK TABLE (FOR README.md)")
    print("=======================================================")
    print("| Scenario | Concurrency | Throughput | p50 | p99 | Balance drift |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        print(f"| {r['name']} | {r['concurrency']} | {r['throughput']} tx/s | {r['p50']} ms | {r['p99']} ms | {drift} |")
    print("=======================================================\n")

if __name__ == '__main__':
    run_benchmark()
