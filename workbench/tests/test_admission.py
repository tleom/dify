"""Real Redis acceptance of the admission scripts under a unique test namespace."""
import os
from uuid import uuid4
from redis import Redis
from services.workbench.scheduler import CLAIM, PUBLISH, RELEASE, stream_entries
from concurrent.futures import ThreadPoolExecutor
import pytest


@pytest.mark.parametrize('protocol', [2, 3])
def test_stream_replay_matches_both_redis_protocols(protocol):
    client=Redis(host=os.environ['REDIS_HOST'],password=os.environ['REDIS_PASSWORD'],protocol=protocol)
    key='workbench-check:'+uuid4().hex
    try:
        first=client.xadd(key, {'data':'first'})
        second=client.xadd(key, {'data':'second'})
        assert list(stream_entries(client.xread({key:'0-0'})))==[(first, {b'data':b'first'}), (second, {b'data':b'second'})]
        assert list(stream_entries(client.xread({key:first})))==[(second, {b'data':b'second'})]
    finally:
        client.delete(key)
        client.close()


def test_fairness_capacity_maintenance_and_expired_quarantine():
    redis=Redis(host=os.environ['REDIS_HOST'],port=int(os.environ['REDIS_PORT']),password=os.environ['REDIS_PASSWORD'])
    prefix='workbench-check:'+uuid4().hex+':'
    try:
        # Ten users, three tasks each; publication retries must not duplicate entries.
        for user in range(10):
            for job in range(3):
                for _ in range(2):
                    redis.eval(PUBLISH,1,prefix,f'u{user}',f'u{user}-j{job}')
        claims=[redis.eval(CLAIM,1,prefix,100,2,20,10) for _ in range(20)]
        assert [claim[0] for claim in claims]==[f'u{u}'.encode() for u in range(10)]*2
        assert redis.eval(CLAIM,1,prefix,100,2,20,10)==[]
        assert redis.scard(prefix+'active_owners')==10
        # Tool input/output events stay in the same run; each run can call many tools.
        for owner,run in claims:
            for tool in range(12):
                redis.xadd(prefix+'events:'+run.decode(), {'tool':str(tool)})
            assert redis.zcard(prefix+'active:'+owner.decode())==2
        assert redis.zcard(prefix+'active')==20
        # Expiry alone must not admit a potentially duplicated external execution.
        assert redis.eval(CLAIM,1,prefix,1000,2,20,10)==[]
        redis.eval(RELEASE,1,prefix,'u0','u0-j0')
        redis.set(prefix+'maintenance:u0','1')
        assert redis.eval(CLAIM,1,prefix,1000,2,20,10)==[]
        redis.delete(prefix+'maintenance:u0')
        assert redis.eval(CLAIM,1,prefix,1000,2,20,10)==[b'u0',b'u0-j2']
        assert redis.llen(prefix+'queue:u0')==0
    finally:
        keys=list(redis.scan_iter(prefix+'*'))
        if keys:
            redis.delete(*keys)
        redis.close()


def test_ten_active_users_and_atomic_concurrent_admission():
    redis=Redis(host=os.environ['REDIS_HOST'],port=int(os.environ['REDIS_PORT']),password=os.environ['REDIS_PASSWORD'])
    prefix='workbench-check:'+uuid4().hex+':'
    try:
        for user in range(11):
            for job in range(2):
                redis.eval(PUBLISH,1,prefix,f'u{user}',f'u{user}-j{job}')
        with ThreadPoolExecutor(max_workers=24) as pool:
            claims=list(pool.map(lambda _:redis.eval(CLAIM,1,prefix,100,2,20,10),range(48)))
        claims=[claim for claim in claims if claim]
        assert len(claims)==20
        assert len({claim[1] for claim in claims})==20
        assert redis.scard(prefix+'active_owners')==10
        # One free task slot is insufficient for user 11 until a user finishes both tasks.
        redis.eval(RELEASE,1,prefix,'u0','u0-j0')
        assert redis.zcard(prefix+'active')==19
        assert redis.eval(CLAIM,1,prefix,100,2,20,10)==[]
        redis.eval(RELEASE,1,prefix,'u0','u0-j1')
        redis.eval(RELEASE,1,prefix,'u0','u0-j1')
        assert redis.scard(prefix+'active_owners')==9
        assert redis.eval(CLAIM,1,prefix,100,2,20,10)==[b'u10',b'u10-j0']
        assert redis.eval(CLAIM,1,prefix,100,2,20,10)==[b'u10',b'u10-j1']
        assert redis.zcard(prefix+'active')==20
    finally:
        keys=list(redis.scan_iter(prefix+'*'))
        if keys:redis.delete(*keys)
        redis.close()
