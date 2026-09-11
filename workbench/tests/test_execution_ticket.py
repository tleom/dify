"""The execution ticket survives run retention and cancellation races in real Redis."""
import asyncio
import json
import os
from uuid import uuid4

from redis.asyncio import Redis
from dify_agent.storage.redis_run_store import RedisRunStore
from dify_agent.storage.redis_keys import run_record_key


def test_duplicate_and_late_tickets_never_schedule_again():
    async def check():
        client=Redis(host=os.environ['REDIS_HOST'],password=os.environ['REDIS_PASSWORD'])
        prefix='workbench-check:'+uuid4().hex
        store=RedisRunStore(client,prefix=prefix)
        try:
            ticket=str(uuid4())
            owner={'epoch':'e'*64,'binding_id':str(uuid4()),'workspace':str(uuid4())}
            results=await asyncio.gather(*(store.create_run_once(ticket,owner) for _ in range(12)))
            assert sum(created for _,created in results)==1
            assert await store.fence_run(ticket)=='running'
            assert json.loads(await client.get(prefix+':ticket:'+ticket))==owner
            await client.delete(run_record_key(prefix,ticket))
            record,created=await store.create_run_once(ticket,owner)
            assert not created and record.status=='cancelled'
            assert await store.fence_run(ticket)=='running', 'Unknown cleanup must remain quarantined'
            late=str(uuid4())
            assert await store.fence_run(late)=='cancelled'
            record,created=await store.create_run_once(late,owner)
            assert not created and record.status=='cancelled'
        finally:
            keys=[key async for key in client.scan_iter(prefix+'*')]
            if keys:await client.delete(*keys)
            await client.aclose()
    asyncio.run(check())
