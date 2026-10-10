import pytest
import json
import hashlib
import threading
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

from django.db import connection, transaction
from django.utils import timezone
import datetime

from ledger.models import Tenant, Account, Hold, OutboxEvent
from ledger.services.sharding import create_sharded_account, get_aggregate_balance
from ledger.services.posting import post_entry
from ledger.services.holds import authorize_hold, void_hold, HoldError
from workers.tasks import expire_holds


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()


@pytest.fixture
def tenant():
    return Tenant.objects.create(name="Holds Fixes Tenant")


@pytest.mark.django_db(transaction=True)
def test_sharded_master_hold_sufficiency(tenant):
    """
    3.a: Sharded master, aggregate 100:
    first authorize_hold(100) is 201, second is rejected with insufficient_funds.
    """
    master = create_sharded_account(
        tenant.id, "Sharded Master", "ASSET", "USD",
        allow_negative=True, shard_count=2
    )
    source = create_sharded_account(
        tenant.id, "Source", "LIABILITY", "USD",
        allow_negative=True, shard_count=0
    )
    shards = list(Account.objects.filter(tenant=tenant, parent_account_id=master.id).order_by('id'))
    assert len(shards) == 2

    # Seed 50 each -> aggregate 100
    post_entry(tenant, {
        'reference_id': 'seed-shard-1',
        'lines': [
            {'account_id': str(source.id), 'amount': '50.0000', 'direction': 'CREDIT'},
            {'account_id': str(shards[0].id), 'amount': '50.0000', 'direction': 'DEBIT'}
        ]
    }, 'idem-seed-1', _hash({'s': 1}), bypass_sharding=True)

    post_entry(tenant, {
        'reference_id': 'seed-shard-2',
        'lines': [
            {'account_id': str(source.id), 'amount': '50.0000', 'direction': 'CREDIT'},
            {'account_id': str(shards[1].id), 'amount': '50.0000', 'direction': 'DEBIT'}
        ]
    }, 'idem-seed-2', _hash({'s': 2}), bypass_sharding=True)

    agg = get_aggregate_balance(tenant.id, master.id)
    assert agg['available_balance'] == Decimal('100.0000')

    # First hold for 100 -> 201
    hp1 = {
        'account_id': str(master.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'hold-sharded-1',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    c1, b1, _ = authorize_hold(tenant, hp1, 'idem-h1', _hash(hp1))
    assert c1 == 201

    # Second hold for 100 -> rejected with insufficient_funds
    hp2 = {
        'account_id': str(master.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'hold-sharded-2',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    with pytest.raises(HoldError, match="insufficient_funds"):
        authorize_hold(tenant, hp2, 'idem-h2', _hash(hp2))


@pytest.mark.django_db(transaction=True)
def test_unsharded_allow_negative_true_overhold(tenant):
    """
    3.b: Unsharded allow_negative=true still allows an over-hold.
    """
    acc = create_sharded_account(
        tenant.id, "Unsharded Overhold Allowed", "ASSET", "USD",
        allow_negative=True, shard_count=0
    )
    # Balance is 0.0000, but allow_negative=True allows hold exceeding balance
    hp1 = {
        'account_id': str(acc.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'overhold-1',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    c1, b1, _ = authorize_hold(tenant, hp1, 'idem-oh-1', _hash(hp1))
    assert c1 == 201

    hp2 = {
        'account_id': str(acc.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'overhold-2',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    c2, b2, _ = authorize_hold(tenant, hp2, 'idem-oh-2', _hash(hp2))
    assert c2 == 201


@pytest.mark.django_db(transaction=True)
def test_unsharded_allow_negative_false_unchanged(tenant):
    """
    3.c: Unsharded allow_negative=false unchanged (rejects over-hold).
    """
    acc = create_sharded_account(
        tenant.id, "Unsharded Strict", "ASSET", "USD",
        allow_negative=False, shard_count=0
    )
    source = create_sharded_account(
        tenant.id, "Strict Source", "LIABILITY", "USD",
        allow_negative=True, shard_count=0
    )
    # Seed 100
    seed_payload = {
        'reference_id': 'seed-strict',
        'lines': [
            {'account_id': str(source.id), 'amount': '100.0000', 'direction': 'CREDIT'},
            {'account_id': str(acc.id), 'amount': '100.0000', 'direction': 'DEBIT'}
        ]
    }
    c_s, _, _ = post_entry(tenant, seed_payload, 'idem-seed-strict', _hash(seed_payload))
    assert c_s == 201

    hp1 = {
        'account_id': str(acc.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'strict-hold-1',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    c1, b1, _ = authorize_hold(tenant, hp1, 'idem-st-1', _hash(hp1))
    assert c1 == 201

    hp2 = {
        'account_id': str(acc.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'strict-hold-2',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    with pytest.raises(HoldError, match="insufficient_funds"):
        authorize_hold(tenant, hp2, 'idem-st-2', _hash(hp2))


@pytest.mark.django_db(transaction=True)
def test_released_amount_in_void_and_expire_outbox(tenant):
    """
    3.d: Void a hold and expire one: outbox payload has released_amount == amount - captured_amount.
    """
    acc = create_sharded_account(
        tenant.id, "Release Test Acc", "ASSET", "USD",
        allow_negative=True, shard_count=0
    )

    # 1. Void hold with partial captured_amount
    hp_void = {
        'account_id': str(acc.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'void-release-hold',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    c_v, b_v, _ = authorize_hold(tenant, hp_void, 'idem-v-auth', _hash(hp_void))
    assert c_v == 201
    hid_v = b_v['hold_id']

    # Update captured_amount directly to simulate partial capture
    Hold.objects.filter(id=hid_v).update(captured_amount=Decimal('40.0000'))

    c_void, b_void, _ = void_hold(tenant, hid_v, 'idem-void-exec', _hash({}))
    assert c_void == 200

    outbox_void = OutboxEvent.objects.filter(tenant=tenant, type='hold.voided', payload__hold_id=hid_v).first()
    assert outbox_void is not None
    assert outbox_void.payload.get('released_amount') == '60.0000'

    # 2. Expire hold with partial captured_amount
    hp_exp = {
        'account_id': str(acc.id),
        'amount': '100.0000',
        'currency': 'USD',
        'reference_id': 'exp-release-hold',
        'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
    }
    c_e, b_e, _ = authorize_hold(tenant, hp_exp, 'idem-e-auth', _hash(hp_exp))
    assert c_e == 201
    hid_e = b_e['hold_id']

    # Set captured_amount to 30 and expires_at to past
    Hold.objects.filter(id=hid_e).update(
        captured_amount=Decimal('30.0000'),
        expires_at=timezone.now() - datetime.timedelta(hours=1)
    )

    expire_holds()

    outbox_exp = OutboxEvent.objects.filter(tenant=tenant, type='hold.expired', payload__hold_id=hid_e).first()
    assert outbox_exp is not None
    assert outbox_exp.payload.get('released_amount') == '70.0000'


@pytest.mark.django_db(transaction=True)
def test_concurrent_hold_on_sharded_master(tenant):
    """
    3.e: One concurrency test: 10 threads, barrier, distinct keys,
    authorize_hold(100) on the sharded master. At most one 201.
    """
    master = create_sharded_account(
        tenant.id, "Conc Master", "ASSET", "USD",
        allow_negative=True, shard_count=2
    )
    source = create_sharded_account(
        tenant.id, "Conc Source", "LIABILITY", "USD",
        allow_negative=True, shard_count=0
    )
    shards = list(Account.objects.filter(tenant=tenant, parent_account_id=master.id).order_by('id'))

    post_entry(tenant, {
        'reference_id': 'seed-conc-1',
        'lines': [
            {'account_id': str(source.id), 'amount': '50.0000', 'direction': 'CREDIT'},
            {'account_id': str(shards[0].id), 'amount': '50.0000', 'direction': 'DEBIT'}
        ]
    }, 'idem-seed-c1', _hash({'c': 1}), bypass_sharding=True)

    post_entry(tenant, {
        'reference_id': 'seed-conc-2',
        'lines': [
            {'account_id': str(source.id), 'amount': '50.0000', 'direction': 'CREDIT'},
            {'account_id': str(shards[1].id), 'amount': '50.0000', 'direction': 'DEBIT'}
        ]
    }, 'idem-seed-c2', _hash({'c': 2}), bypass_sharding=True)

    barrier = threading.Barrier(10, timeout=10)
    results = []
    lock = threading.Lock()

    def worker(i):
        try:
            barrier.wait()
            payload = {
                'account_id': str(master.id),
                'amount': '100.0000',
                'currency': 'USD',
                'reference_id': f'conc-auth-{i}',
                'expires_at': (timezone.now() + datetime.timedelta(hours=1)).isoformat()
            }
            c, b, _ = authorize_hold(tenant, payload, f'idem-conc-auth-{i}', _hash(payload))
            with lock:
                results.append(c)
        except Exception as e:
            with lock:
                results.append(str(e))

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker, i) for i in range(10)]
        for f in futures:
            f.result()

    count_201 = results.count(201)
    print(f"\nConcurrency Test 201 Count: {count_201}")
    print(f"Results: {results}")

    assert count_201 <= 1, f"More than one 201 occurred: {count_201}"
