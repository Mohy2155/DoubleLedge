from django.db import connection

from ledger.models import IdempotencyKey


def get_or_insert_idempotency_key(tenant_id, key, request_hash):
    """
    Inserts an idempotency key if it doesn't exist.
    If a concurrent request is processing the same key, this will block on the DB lock
    until the first request finishes, then return the existing key.
    
    Returns (key_instance, created)
    """
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO idempotency_keys (tenant_id, key, request_hash, created_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (tenant_id, key) DO NOTHING
            RETURNING key
        """, [tenant_id, key, request_hash])
        created = bool(cursor.fetchone())
        
    instance = IdempotencyKey.objects.get(tenant_id=tenant_id, key=key)
    return instance, created
