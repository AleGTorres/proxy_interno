"""
Proxy interno — versão final para submissão (RF1 + Missões 1-3).
- Enfileira requisições, envia ordenadamente para /score do upstream.
- Taxa máxima: 1 req/s (configurável).
- Cache persistente (SQLite) para 'memorizar' respostas anteriores.
- Política de fila: quando cheia, remove o item de menor prioridade (eviction).
- Circuit breaker + retries com backoff.
- Observability: /metrics (Prometheus) e /health.
- Padrões aplicados: Command, Singleton, Decorator (Cache), Observer (EventBus), Iterator (fila).
"""
import os
import time
import math
import uuid
import asyncio
import heapq
import logging
import sqlite3
import json
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, List

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "https://score.hsborges.dev")
QUEUE_MAXSIZE = int(os.getenv("QUEUE_MAXSIZE", "200"))
RATE_BASE_INTERVAL = float(os.getenv("RATE_BASE_INTERVAL", "1.0"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15.0"))
SIM_UPSTREAM = os.getenv("SIM_UPSTREAM", "1") == "1"
CACHE_DB = os.getenv("CACHE_DB", "proxy_cache.db")
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
CB_FAIL_THRESHOLD = int(os.getenv("CB_FAIL_THRESHOLD", "5"))
CB_OPEN_SECONDS = int(os.getenv("CB_OPEN_SECONDS", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "0.5"))
QUEUE_EVICT_LOWEST = True 

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("proxy_final")

M_REQ_RECEIVED = Counter("proxy_requests_received_total", "Requests received")
M_REQ_ENQUEUED = Counter("proxy_requests_enqueued_total", "Requests enqueued")
M_REQ_PROCESSED = Counter("proxy_requests_processed_total", "Requests sent upstream")
M_REQ_DROPPED = Counter("proxy_requests_dropped_total", "Requests dropped/expired")
M_QUEUE_SIZE = Gauge("proxy_queue_size", "Current queue size")
M_UPSTREAM_LATENCY = Histogram("proxy_upstream_latency_seconds", "Upstream latency")
M_CACHE_HITS = Counter("proxy_cache_hits_total", "Cache hits")
M_CACHE_MISSES = Counter("proxy_cache_misses_total", "Cache misses")
M_CIRCUIT_OPEN = Gauge("proxy_circuit_open", "1 when circuit open else 0")

@dataclass(order=True)
class ProxyCommand:
    priority: int
    seq: int
    method: str = field(compare=False)
    path: str = field(compare=False)
    params: Dict[str, Any] = field(compare=False)
    ttl: float = field(default=30.0, compare=False)
    enqueued_at: float = field(default_factory=time.time, compare=False)
    future: asyncio.Future = field(default=None, compare=False, repr=False)

    def is_expired(self) -> bool:
        return (time.time() - self.enqueued_at) > self.ttl

    def key(self) -> str:
        items = sorted(self.params.items())
        return f"{self.path}?{items}"

class SQLiteCache:
    def __init__(self, db_path: str = CACHE_DB, ttl: int = CACHE_TTL):
        self.db_path = db_path
        self.ttl = ttl
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            ts INTEGER,
            value TEXT
        )
        """)
        conn.commit()
        conn.close()

    def _now(self) -> int:
        return int(time.time())

    async def get(self, key: str) -> Optional[Dict]:
        def _get_sync(k):
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT ts, value FROM cache WHERE key = ?", (k,))
            row = cur.fetchone()
            conn.close()
            return row
        row = await asyncio.to_thread(_get_sync, key)
        if not row:
            M_CACHE_MISSES.inc()
            return None
        ts, val = row
        if self._now() - ts > self.ttl:
            await self.delete(key)
            M_CACHE_MISSES.inc()
            return None
        M_CACHE_HITS.inc()
        try:
            return json.loads(val)
        except Exception:
            return None

    async def set(self, key: str, value: Dict):
        def _set_sync(k, ts, v):
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO cache (key, ts, value) VALUES (?, ?, ?)", (k, ts, v))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_set_sync, key, self._now(), json.dumps(value))

    async def delete(self, key: str):
        def _del_sync(k):
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM cache WHERE key = ?", (k,))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_del_sync, key)

    async def clear(self):
        def _clear_sync():
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM cache")
            conn.commit()
            conn.close()
        await asyncio.to_thread(_clear_sync)

class UpstreamClient:
    _instance = None

    def __init__(self):
        self.client = httpx.AsyncClient(base_url=UPSTREAM_BASE, timeout=REQUEST_TIMEOUT)

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = UpstreamClient()
        return cls._instance

    async def get(self, path: str, params: Dict[str, Any]):
        if SIM_UPSTREAM:
            delay = random.choice([0.05, 0.08, 0.12, 2.8, 3.2])
            await asyncio.sleep(delay)
            if random.random() < 0.08:
                raise httpx.HTTPError("simulated upstream error")
            return {"simulated": True, "path": path, "params": params, "delay": delay}
        resp = await self.client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

class CircuitBreaker:
    def __init__(self, fail_threshold=CB_FAIL_THRESHOLD, open_seconds=CB_OPEN_SECONDS):
        self.fail_threshold = fail_threshold
        self.open_seconds = open_seconds
        self._fail_count = 0
        self._open_until = 0.0
        self._lock = asyncio.Lock()

    def is_open(self) -> bool:
        return time.time() < self._open_until

    async def record_success(self):
        async with self._lock:
            self._fail_count = 0

    async def record_failure(self):
        async with self._lock:
            self._fail_count += 1
            if self._fail_count >= self.fail_threshold:
                self._open_until = time.time() + self.open_seconds
                logger.warning(f"Circuit opened until {self._open_until}")

class EvictingPriorityQueue:
    def __init__(self, maxsize: int = 0):
        self._heap: List[Tuple[int,int,ProxyCommand]] = []
        self._maxsize = maxsize
        self._cond = asyncio.Condition()
        self._size = 0

    def qsize(self) -> int:
        return self._size

    async def put(self, cmd: ProxyCommand) -> bool:
        async with self._cond:
            if self._maxsize and self._size >= self._maxsize:
                worst = max(self._heap, key=lambda x: (x[0], x[1]))
                worst_prio, worst_seq, worst_cmd = worst
                if (cmd.priority, cmd.seq) < (worst_prio, worst_seq):
                    self._heap.remove(worst)
                    heapq.heapify(self._heap)
                    heapq.heappush(self._heap, (cmd.priority, cmd.seq, cmd))
                    M_REQ_DROPPED.inc()
                    logger.info(f"Evicted cmd seq={worst_seq} prio={worst_prio} for new seq={cmd.seq} prio={cmd.priority}")
                    self._cond.notify()
                    return True
                else:
                    return False
            else:
                heapq.heappush(self._heap, (cmd.priority, cmd.seq, cmd))
                self._size += 1
                M_QUEUE_SIZE.set(self._size)
                self._cond.notify()
                return True

    async def get(self) -> ProxyCommand:
        async with self._cond:
            while self._size == 0:
                await self._cond.wait()
            prio, seq, cmd = heapq.heappop(self._heap)
            self._size -= 1
            M_QUEUE_SIZE.set(self._size)
            return cmd

class EventBus:
    def __init__(self):
        self._subs = {}

    def subscribe(self, event_name: str, coro):
        self._subs.setdefault(event_name, []).append(coro)

    async def publish(self, event_name: str, payload: Dict):
        for coro in self._subs.get(event_name, []):
            try:
                await coro(payload)
            except Exception:
                logger.exception("Event handler error")

class RateLimiter:
    def __init__(self, interval=RATE_BASE_INTERVAL):
        self.interval = interval
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._penalty_until = 0.0

    async def wait_next(self):
        async with self._lock:
            now = time.time()
            next_allowed = self._last + self.interval
            if time.time() < self._penalty_until:
                next_allowed = max(next_allowed, self._last + self.interval + 2.0)
            wait = max(0.0, next_allowed - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.time()

    def apply_penalty(self, seconds=2.0):
        self._penalty_until = max(self._penalty_until, time.time() + seconds)
        logger.warning(f"Penalty applied until {self._penalty_until}")

class ProxyService:
    def __init__(self):
        self.seq = 0
        self.queue = EvictingPriorityQueue(maxsize=QUEUE_MAXSIZE)
        self.rate_limiter = RateLimiter()
        self.cache = SQLiteCache(db_path=CACHE_DB, ttl=CACHE_TTL)
        self.upstream = UpstreamClient.instance()
        self.circuit = CircuitBreaker()
        self.event_bus = EventBus()
        self.worker_task = None
        self._running = False
        self.event_bus.subscribe("request_enqueued", self._on_enqueued)
        self.event_bus.subscribe("request_processed", self._on_processed)

    async def _on_enqueued(self, payload):
        logger.debug(f"ENQUEUED: {payload}")

    async def _on_processed(self, payload):
        logger.debug(f"PROCESSED: {payload}")

    async def start(self):
        if not self._running:
            self._running = True
            self.worker_task = asyncio.create_task(self._worker_loop())
            logger.info("ProxyService started")

    async def stop(self):
        self._running = False
        if self.worker_task:
            self.worker_task.cancel()
            with suppress_cancel():
                await self.worker_task

    async def enqueue(self, method: str, path: str, params: Dict[str, Any], priority: int = 10, ttl: int = 30):
        M_REQ_RECEIVED.inc()
        fut = asyncio.get_event_loop().create_future()
        self.seq += 1
        cmd = ProxyCommand(priority=priority, seq=self.seq, method=method, path=path, params=params, ttl=ttl, future=fut)
        ok = await self.queue.put(cmd)
        if ok:
            M_REQ_ENQUEUED.inc()
            await self.event_bus.publish("request_enqueued", {"seq": cmd.seq, "priority": cmd.priority})
            return fut
        else:
            key = cmd.key()
            cached = await self.cache.get(key)
            if cached:
                M_CACHE_HITS.inc()
                fut.set_result({"from_cache": True, "payload": cached})
                return fut
            M_REQ_DROPPED.inc()
            fut.set_exception(HTTPException(status_code=503, detail="Queue full - rejected"))
            return fut

    async def _worker_loop(self):
        while self._running:
            try:
                cmd: ProxyCommand = await self.queue.get()
                if cmd.is_expired():
                    M_REQ_DROPPED.inc()
                    if not cmd.future.done():
                        cmd.future.set_exception(HTTPException(status_code=504, detail="TTL expired"))
                    continue

                if self.circuit.is_open():
                    M_CIRCUIT_OPEN.set(1)
                    key = cmd.key()
                    cached = await self.cache.get(key)
                    if cached:
                        if not cmd.future.done():
                            cmd.future.set_result({"from_cache": True, "payload": cached})
                        continue
                    else:
                        if not cmd.future.done():
                            cmd.future.set_exception(HTTPException(status_code=503, detail="Upstream unavailable (circuit open)"))
                        continue
                else:
                    M_CIRCUIT_OPEN.set(0)

                await self.rate_limiter.wait_next()

                key = cmd.key()
                cached = await self.cache.get(key)
                if cached:
                    if not cmd.future.done():
                        cmd.future.set_result({"from_cache": True, "payload": cached})
                    M_CACHE_HITS.inc()
                    await self.event_bus.publish("request_processed", {"seq": cmd.seq, "from_cache": True})
                    continue
                else:
                    M_CACHE_MISSES.inc()

                attempt = 0
                last_exc = None
                while attempt <= MAX_RETRIES:
                    try:
                        start = time.time()
                        with M_UPSTREAM_LATENCY.time():
                            resp = await self.upstream.get(cmd.path, cmd.params)
                        latency = time.time() - start
                        M_REQ_PROCESSED.inc()
                        if latency > 2.5:
                            self.rate_limiter.apply_penalty(2.0)
                        await self.cache.set(key, resp)
                        if not cmd.future.done():
                            cmd.future.set_result(resp)
                        await self.circuit.record_success()
                        await self.event_bus.publish("request_processed", {"seq": cmd.seq, "latency": latency})
                        break
                    except Exception as e:
                        last_exc = e
                        await self.circuit.record_failure()
                        attempt += 1
                        if attempt > MAX_RETRIES:
                            logger.exception("Upstream final failure")
                            if not cmd.future.done():
                                cmd.future.set_exception(HTTPException(status_code=502, detail=str(e)))
                            break
                        backoff = BACKOFF_BASE * (2 ** (attempt - 1))
                        backoff = backoff * (1 + (random.random() - 0.5) * 0.3)
                        await asyncio.sleep(backoff)
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker loop error (continuing)")

class suppress_cancel:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        return exc_type is asyncio.CancelledError

from fastapi import FastAPI
app = FastAPI(title="Proxy final - RF1 + Missões 1-3")
service = ProxyService()

@app.on_event("startup")
async def startup():
    await service.start()

@app.on_event("shutdown")
async def shutdown():
    await service.stop()

@app.get("/proxy/score")
async def proxy_score(q: Optional[str] = Query(None), id: Optional[str] = Query(None),
                      priority: int = Query(10), ttl: int = Query(30)):
    params = {"q": q, "id": id}
    fut = await service.enqueue("GET", "/score", params, priority=priority, ttl=ttl)
    try:
        result = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT + 5.0)
        return JSONResponse(content={"ok": True, "data": result})
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout waiting proxy resolution")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return PlainTextResponse(data.decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queue_size": service.queue.qsize(),
        "circuit_open": service.circuit.is_open()
    }
