import time

import redis
from django.conf import settings
from redis.exceptions import RedisError
from rest_framework.throttling import BaseThrottle

# We initialize it lazily or at module level? Module level is fine since settings.REDIS_URL should be available
try:
    redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
except AttributeError:
    # Fallback if settings.REDIS_URL is not set yet
    redis_client = None

TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local bucket = redis.call('HMGET', key, 'tokens', 'last_update')
local tokens = tonumber(bucket[1])
local last_update = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_update = now
else
    local elapsed = now - last_update
    local added_tokens = elapsed * rate
    tokens = math.min(capacity, tokens + added_tokens)
end

if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_update', now)
    redis.call('EXPIRE', key, math.ceil(capacity / rate) + 1)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_update', last_update)
    return 0
end
"""

class TenantRateThrottle(BaseThrottle):
    capacity = 100
    rate = 10

    def allow_request(self, request, view):
        if not request.user or not hasattr(request.user, 'id'):
            return True
            
        tenant_id = str(request.user.id)
        key = f"rate_limit:{tenant_id}"
        now = time.time()
        
        global redis_client
        if not redis_client:
            redis_client = redis.Redis.from_url(getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)
            
        try:
            script = redis_client.register_script(TOKEN_BUCKET_SCRIPT)
            allowed = script(keys=[key], args=[self.capacity, self.rate, now])
            return bool(allowed)
        except RedisError:
            # Fail open
            return True
