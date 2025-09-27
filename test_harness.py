import asyncio
import time
import httpx
import json

TARGET = "http://localhost:8000/proxy/score"

async def send_one(session, idx, id_val="abc", priority=10):
    try:
        r = await session.get(TARGET, params={"id": id_val, "q": f"q{idx}", "priority": priority})
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body
    except Exception as e:
        return 0, str(e)

async def burst_test(n=20):
    async with httpx.AsyncClient(timeout=60) as client:
        tasks = [asyncio.create_task(send_one(client, i, id_val=str(i), priority=(i%5)+1)) for i in range(n)]
        res = await asyncio.gather(*tasks, return_exceptions=True)
        return res

async def cache_test():
    async with httpx.AsyncClient(timeout=60) as client:
        s1 = await send_one(client, 1, id_val="CACHE_ME", priority=5)
        s2 = await send_one(client, 2, id_val="CACHE_ME", priority=5)
        return s1, s2

async def run_all():
    print("Starting burst test (20 req) ...")
    t0 = time.time()
    res = await burst_test(20)
    t1 = time.time()
    print("Elapsed:", round(t1 - t0, 2))
    ok = sum(1 for r in res if isinstance(r, tuple) and r[0] == 200)
    print(f"Success: {ok}/{len(res)}")
    for i, r in enumerate(res[:5]):
        if isinstance(r, tuple):
            print(i, r[0], str(r[1])[:200])
        else:
            print(i, "EXC", str(r)[:200])

    print("\nNow cache test ...")
    s1, s2 = await cache_test()
    print("first status:", s1[0], "body (truncated):", str(s1[1])[:300])
    print("second status:", s2[0], "body (truncated):", str(s2[1])[:300])

if __name__ == "__main__":
    asyncio.run(run_all())
