"""Admit whole runs, never individual tool calls, with per-user and global leases."""

import time

from configs import dify_config
from extensions.ext_redis import redis_client

PREFIX = "workbench:v1:"

PUBLISH = """
if redis.call('SADD', KEYS[1]..'enqueued', ARGV[2]) == 1 then
 redis.call('RPUSH', KEYS[1]..'queue:'..ARGV[1], ARGV[2])
 if redis.call('SADD', KEYS[1]..'owners', ARGV[1]) == 1 then redis.call('RPUSH', KEYS[1]..'round', ARGV[1]) end
end
return 1
"""
CLAIM = """
local p = KEYS[1]
-- Expired leases remain counted until the controller fences the remote execution.
if redis.call('ZCARD', p..'active') >= tonumber(ARGV[3]) then return {} end
local n = redis.call('LLEN', p..'round')
for i=1,n do
 local owner = redis.call('LPOP', p..'round')
 local q = p..'queue:'..owner
 if redis.call('LLEN', q) == 0 then redis.call('SREM', p..'owners', owner)
 else
  redis.call('RPUSH', p..'round', owner)
  local owned = redis.call('ZCARD', p..'active:'..owner)
  if owned < tonumber(ARGV[2])
     and (owned > 0 or redis.call('SCARD', p..'active_owners') < tonumber(ARGV[4]))
     and redis.call('EXISTS', p..'maintenance:'..owner) == 0 then
   local id = redis.call('LPOP', q)
   redis.call('SREM', p..'enqueued', id)
   redis.call('ZADD', p..'active', tonumber(ARGV[1])+90, id)
   redis.call('ZADD', p..'active:'..owner, tonumber(ARGV[1])+90, id)
   redis.call('SADD', p..'active_owners', owner)
   return {owner,id}
  end
 end
end
return {}
"""
RELEASE = """
local p = KEYS[1]
redis.call('ZREM', p..'active', ARGV[2])
redis.call('ZREM', p..'active:'..ARGV[1], ARGV[2])
if redis.call('ZCARD', p..'active:'..ARGV[1]) == 0 then
 redis.call('SREM', p..'active_owners', ARGV[1])
end
return 1
"""


def publish(tenant_id, account_id, run_id):
    redis_client.eval(PUBLISH, 1, PREFIX, f"{tenant_id}:{account_id}", run_id)
    from tasks.workbench_tasks import dispatch

    dispatch.delay()


def claim():
    value = redis_client.eval(
        CLAIM,
        1,
        PREFIX,
        time.time(),
        dify_config.WORKBENCH_PER_USER_RUNS,
        dify_config.WORKBENCH_GLOBAL_RUNS,
        dify_config.WORKBENCH_MAX_ACTIVE_USERS,
    )
    return [x.decode() if isinstance(x, bytes) else x for x in value]


def heartbeat(owner, run_id):
    # Never revive an expired lease; the executor must stop if its ownership was lost.
    return redis_client.eval(
        """
    local expires = redis.call('ZSCORE', KEYS[1]..'active', ARGV[1])
    if not expires or tonumber(expires) < tonumber(ARGV[4]) then return 0 end
    redis.call('ZADD', KEYS[1]..'active', 'XX', ARGV[3], ARGV[1])
    redis.call('ZADD', KEYS[1]..'active:'..ARGV[2], 'XX', ARGV[3], ARGV[1])
    return 1
    """,
        1,
        PREFIX,
        run_id,
        owner,
        time.time() + 90,
        time.time(),
    )


def release(owner, run_id):
    redis_client.eval(RELEASE, 1, PREFIX, owner, run_id)


def event_key(run_id):
    return PREFIX + "events:" + run_id


def stream_entries(response):
    """Normalize redis-py XREAD replies from both RESP2 and RESP3 connections."""
    if isinstance(response, dict):
        for batches in response.values():
            for entries in batches:
                yield from entries
    else:
        for _, entries in response or []:
            yield from entries
