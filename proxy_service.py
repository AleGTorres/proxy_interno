# proxy_service.py
import os
import time
import uuid
import asyncio
import heapq
import logging
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "https://score.hsborges.dev")
QUEUE_MAXSIZE = int(os.getenv("QUEUE_MAXSIZE", "200"))
QUEUE_POLICY = os.getenv("QUEUE_POLICY", "fifo")
DEGRADE_POLICY = os.getenv("DEGRADE_POLICY", "reject")
BASE_INTERVAL = float(os.getenv("RATE_BASE_INTERVAL", "1.0"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15.0"))
CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CIRCUIT_OPEN_SECONDS = int(os.getenv("CB_OPEN_SECONDS", "10"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))
SIMULATE_UPSTREAM_SAFELY = os.getenv("SIM_UPSTREAM", "1") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("proxy")

M_REQ_RECEIVED = Counter("proxy_requests_received_total", "Requests received by proxy")
M_REQ_ENQUEUED = Counter("proxy_requests_enqueued_total", "Requests enqueued")
M_REQ_PROCESSED = Counter("proxy_requests_processed_total", "Requests sent to upstream")
M_REQ_DROPPED = Counter("proxy_requests_dropped_total", "Requests dropped (TTL or shed)")
M_PENALTIES_DETECTED = Counter("proxy_penalties_detected_total", "Penalties detected from upstream")
M_PENALTIES_AVOIDED = Counter("proxy_penalties_avoided_total", "Penalties avoided thanks to proxy")
M_QUEUE_SIZE = Gauge("proxy_queue_size", "Current queue size")
M_UPSTREAM_LATENCY = Histogram("proxy_upstream_latency_seconds", "Upstream request latency seconds")


@dataclass(order=True)
class ProxyCommand:
    """
    Command pattern: encapsula uma requisição interna.
    - order=True permite usar em priority queue (por priority, seq)
    """
    sort_index: int = field(init=False, repr=False)
    priority: int
    seq: int
    method: str
    path: str
    params: Dict[str, Any]
    ttl: float = 30.0
    enqueued_at: float = field(default_factory=time.time)
    future: asyncio.Future = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        self.sort_index = (self.priority, self.seq)

    def is_expired(self) -> bool:
        return (time.time() - self.enqueued_at) > self.ttl

class AsyncPriorityQueue:
    def __init__(self, maxsize: int = 0):
        self._heap = []
        self._maxsize = maxsize
        self._cond = asyncio.Condition()
        self._size = 0

    def qsize(self):
        return self._size

    async def put_nowait(self, cmd: ProxyCommand) -> bool:
        async with self._cond:
            if self._maxsize and self._size >= self._maxsize:
                return False
            heapq.heappush(self._heap, (cmd.priority, cmd.seq, cmd))
            self._size += 1
            M_QUEUE_SIZE.set(self._size)
            self._cond.notify()
            return True

    async def get(self) -> ProxyCommand:
        async with self._cond:
            while self._size == 0:
                await self._cond.wait()
            _, _, cmd = heapq.heappop(self._heap)
            self._size -= 1
            M_QUEUE_SIZE.set(self._size)
            return cmd

class AdaptiveRateLimiter:
    def __init__(self, base_interval: float = 1.0):
        self.base_interval = base_interval
        self._last_call = 0.0
        self._penalty_until = 0.0
        self._lock = asyncio.Lock()

    async def wait_next(self):
        async with self._lock:
            now = time.time()
            next_allowed = self._last_call + self.base_interval
            if time.time() < self._penalty_until:
                # during penalty, extend interval
                next_allowed = max(next_allowed, self._last_call + self.base_interval + 2.0)
            wait = max(0.0, next_allowed - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()

    def apply_penalty(self, seconds: float = 2.0):
        self._penalty_until = max(self._penalty_until, time.time() + seconds)
        M_PENALTIES_DETECTED.inc()
        logger.warning(f"Applied penalty: next emissions postponed until {self._penalty_until}")

class SimpleCache:
    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}
        self._lock = asyncio.Lock()

    def _key(self, path: str, params: Dict[str, Any]):
        # very simple cache key
        items = sorted(params.items())
        return f"{path}?{items}"

    async def get(self, path: str, params: Dict[str, Any]) -> Optional[Dict]:
        key = self._key(path, params)
        async with self._lock:
            v = self._store.get(key)
            if not v:
                return None
            ts, value = v
            if time.time() - ts > self._ttl:
                del self._store[key]
                return None
            return value

    async def set(self, path: str, params: Dict[str, Any], value: Dict):
        key = self._key(path, params)
        async with self._lock:
            self._store[key] = (time.time(), value)

class SimpleCircuitBreaker:
    def __init__(self, fail_threshold: int = 5, open_seconds: int = 10):
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

class UpstreamClient:
    _instance = None

    def __init__(self):
        self._client = httpx.AsyncClient(base_url=UPSTREAM_BASE, timeout=REQUEST_TIMEOUT)

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = UpstreamClient()
        return cls._instance

    async def get(self, path: str, params: Dict[str, Any]):
        if SIMULATE_UPSTREAM_SAFELY:
            await asyncio.sleep(0.05)
            return {"simulated": True, "path": path, "params": params}
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

class ProxyService:
    def __init__(self):
        self.seq = 0
        self.queue = AsyncPriorityQueue(maxsize=QUEUE_MAXSIZE)
        self.rate_limiter = AdaptiveRateLimiter(base_interval=BASE_INTERVAL)
        self.cache = SimpleCache(ttl_seconds=CACHE_TTL)
        self.circuit = SimpleCircuitBreaker(
            fail_threshold=CIRCUIT_FAILURE_THRESHOLD, open_seconds=CIRCUIT_OPEN_SECONDS
        )
        self.upstream = UpstreamClient.instance()
        self.worker_task = None
        self._running = False

    async def start(self):
        if not self._running:
            self._running = True
            self.worker_task = asyncio.create_task(self._worker_loop())
            logger.info("ProxyService started worker loop")

    async def stop(self):
        self._running = False
        if self.worker_task:
            self.worker_task.cancel()
            with suppress_cancel():
                await self.worker_task

    async def enqueue_request(self, method: str, path: str, params: Dict[str, Any], priority: int = 10, ttl: float = 30.0):
        M_REQ_RECEIVED.inc()
        fut = asyncio.get_event_loop().create_future()
        self.seq += 1
        cmd = ProxyCommand(priority=priority, seq=self.seq, method=method, path=path, params=params, ttl=ttl, future=fut)
        inserted = await self.queue.put_nowait(cmd)
        if inserted:
            M_REQ_ENQUEUED.inc()
            logger.info(f"Enqueued cmd {cmd.seq} prio={cmd.priority} path={path}")
            return fut
        else:
            M_REQ_DROPPED.inc()
            logger.warning("Queue full: applying degrade policy")
            if DEGRADE_POLICY == "cached":
                cached = await self.cache.get(path, params)
                if cached:
                    M_PENALTIES_AVOIDED.inc()
                    fut.set_result({"from_cache": True, "payload": cached})
                    return fut
                else:
                    fut.set_exception(HTTPException(status_code=503, detail="Queue full and no cached response"))
                    return fut
            elif DEGRADE_POLICY == "reject":
                fut.set_exception(HTTPException(status_code=503, detail="Queue full - rejected by proxy"))
                return fut
            else:
                fut.set_exception(HTTPException(status_code=503, detail="Queue full"))
                return fut

    async def _worker_loop(self):
        while self._running:
            try:
                cmd: ProxyCommand = await self.queue.get()
                if cmd.is_expired():
                    M_REQ_DROPPED.inc()
                    if not cmd.future.done():
                        cmd.future.set_exception(HTTPException(status_code=504, detail="TTL expired in queue"))
                    continue

                if self.circuit.is_open():
                    logger.warning("Circuit breaker open: returning fallback/cached immediately")
                    M_REQ_DROPPED.inc()
                    if not cmd.future.done():
                        # fallback
                        cached = await self.cache.get(cmd.path, cmd.params)
                        if cached:
                            cmd.future.set_result({"from_cache": True, "payload": cached})
                        else:
                            cmd.future.set_exception(HTTPException(status_code=503, detail="Upstream unavailable (circuit open)"))
                    continue

                await self.rate_limiter.wait_next()

                start = time.time()
                try:
                    with M_UPSTREAM_LATENCY.time():
                        resp = await self.upstream.get(cmd.path, cmd.params)
                    latency = time.time() - start
                    M_REQ_PROCESSED.inc()
                    if latency > 2.0 + 0.5:
                        self.rate_limiter.apply_penalty(2.0)
                    await self.cache.set(cmd.path, cmd.params, resp)
                    if not cmd.future.done():
                        cmd.future.set_result(resp)
                    await self.circuit.record_success()
                except Exception as e:
                    await self.circuit.record_failure()
                    logger.exception("Upstream call failed")
                    if not cmd.future.done():
                        cmd.future.set_exception(HTTPException(status_code=502, detail=str(e)))
                finally:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker loop error (continuing)")

class suppress_cancel:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        return exc_type is asyncio.CancelledError

app = FastAPI(title="Proxy interno - base")

service = ProxyService()

@app.on_event("startup")
async def on_startup():
    await service.start()

@app.on_event("shutdown")
async def on_shutdown():
    await service.stop()

@app.get("/proxy/score")
async def proxy_score(q: Optional[str] = Query(None), id: Optional[str] = Query(None), priority: int = Query(10), ttl: int = Query(30)):
    """
    RF1: endpoint principal. Encapsula a requisição em um Command e enfileira.
    O cliente aguardará a resolução (ou timeout).
    Query params são repassados para o upstream.
    """
    params = {"q": q, "id": id}
    fut = await service.enqueue_request("GET", "/score", params, priority=priority, ttl=ttl)
    try:
        result = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT + 5.0)
        return JSONResponse(content={"ok": True, "data": result})
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout waiting for proxy to resolve")
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics")
async def metrics():
    """
    RF2: Prometheus metrics.
    """
    data = generate_latest()
    return PlainTextResponse(data.decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
async def health():
    """
    RF3: liveness/readiness
    """
    qsize = service.queue.qsize()
    return {
        "status": "ok",
        "queue_size": qsize,
        "circuit_open": service.circuit.is_open()
    }
