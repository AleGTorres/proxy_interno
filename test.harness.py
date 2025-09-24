# test_harness.py
import asyncio
import time
import httpx

TARGET = "http://localhost:8000/proxy/score"

async def send_one(session, idx):
    try:
        r = await session.get(TARGET, params={"id": idx})
        text = await r.text()
        return r.status_code, text
    except Exception as e:
        return 0, str(e)

async def burst(n=20, concurrency=20):
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = []
        for i in range(n):
            tasks.append(asyncio.create_task(send_one(client, i)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

if __name__ == "__main__":
    t0 = time.time()
    res = asyncio.run(burst(20))
    t1 = time.time()
    print(f"Elapsed {t1-t0:.2f}s")
    succ = sum(1 for r in res if isinstance(r, tuple) and r[0] == 200)
    print(f"Success: {succ}/{len(res)}")
    for i, r in enumerate(res):
        print(i, r[0] if isinstance(r, tuple) else "ERR", str(r[1])[:120])
