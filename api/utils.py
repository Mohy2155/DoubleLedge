import hashlib
import json


def generate_idempotency_hash(method: str, path: str, body: bytes) -> bytes:
    """
    Generate SHA-256 hash of METHOD + PATH + CANONICAL_BODY.
    Body is parsed and re-serialized canonically: UTF-8, sorted keys, compact separators.
    """
    if not body:
        canonical_body = b""
    else:
        try:
            data = json.loads(body)
            # Re-serialize canonically
            canonical_body = json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')
        except json.JSONDecodeError:
            canonical_body = body
            
    payload = f"{method.upper()}|{path}|".encode() + canonical_body
    return hashlib.sha256(payload).digest()
