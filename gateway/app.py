"""Computer Runtime Gateway — model-agnostic session lease + observe/act proxy."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from pool import SessionPool

logger = logging.getLogger("computer.gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_TTL = int(os.environ.get("SESSION_TTL_SEC", "600"))
PARKED_TTL = int(os.environ.get("PARKED_TTL_SEC", "1800"))
PUBLIC_BASE = os.environ.get("GATEWAY_PUBLIC_BASE", "http://localhost:9000").rstrip("/")
API_TOKEN = os.environ.get("COMPUTER_API_TOKEN", "").strip()


def _parse_workers() -> list[tuple[str, str]]:
    """WORKER_URLS and WORKER_VNC_URLS are comma-separated parallel lists."""
    urls = [u.strip() for u in os.environ.get("WORKER_URLS", "http://127.0.0.1:8000").split(",") if u.strip()]
    vncs_raw = os.environ.get("WORKER_VNC_URLS", "").strip()
    if vncs_raw:
        vncs = [v.strip() for v in vncs_raw.split(",")]
    else:
        vncs = [f"http://127.0.0.1:{6080 + i}/vnc.html" for i in range(len(urls))]
    if len(vncs) != len(urls):
        raise RuntimeError(
            f"WORKER_URLS ({len(urls)}) and WORKER_VNC_URLS ({len(vncs)}) length mismatch"
        )
    return list(zip(urls, vncs, strict=True))


pool = SessionPool(
    _parse_workers(),
    default_ttl_sec=DEFAULT_TTL,
    parked_ttl_sec=PARKED_TTL,
)

_security = HTTPBearer(auto_error=False)


def _reaper_loop(stop: threading.Event) -> None:
    while not stop.wait(15):
        # Lease TTL → park (no reset). Parked idle TTL → free + Firefox reset.
        for worker_url in pool.sweep_expired():
            logger.info("parked idle TTL; recycling worker %s", worker_url)
            pool.reset_worker(worker_url)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    t = threading.Thread(target=_reaper_loop, args=(stop,), daemon=True, name="lease-reaper")
    t.start()
    if not API_TOKEN:
        logger.warning("COMPUTER_API_TOKEN unset — gateway auth is disabled (dev only)")
    logger.info("gateway ready: %d workers, public_base=%s", pool.worker_count, PUBLIC_BASE)
    yield
    stop.set()


app = FastAPI(
    title="Computer Runtime Gateway",
    version="0.2.0",
    description=(
        "Model-agnostic desktop sessions: lease a worker, screenshot/act, soft-release "
        "(park warm Firefox), acquire or job_id-reattach, recycle to wipe. "
        "Inference stays outside — any agent can bind via this OpenAPI."
    ),
    lifespan=lifespan,
)


def require_auth(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> None:
    if not API_TOKEN:
        return
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


AuthDep = Annotated[None, Depends(require_auth)]


class LeaseRequest(BaseModel):
    agent_id: str | None = None
    job_id: str | None = None
    ttl_sec: int | None = Field(default=None, ge=30, le=86400)


class LeaseResponse(BaseModel):
    session_id: str
    api_base: str
    vnc_url: str
    expires_at: datetime
    agent_id: str | None = None
    job_id: str | None = None


class HeartbeatRequest(BaseModel):
    ttl_sec: int | None = Field(default=None, ge=30, le=86400)


class AcquireRequest(BaseModel):
    ttl_sec: int | None = Field(default=None, ge=30, le=86400)
    agent_id: str | None = None


class ActionBody(BaseModel):
    type: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    direction: str = "down"
    amount: int = 3


def _api_base(session_id: str) -> str:
    return f"{PUBLIC_BASE}/v1/sessions/{session_id}"


def _lease_response(info) -> LeaseResponse:
    return LeaseResponse(
        session_id=info.session_id,
        api_base=_api_base(info.session_id),
        vnc_url=info.vnc_url,
        expires_at=info.expires_at,
        agent_id=info.agent_id,
        job_id=info.job_id,
    )


@app.middleware("http")
async def job_id_log(request: Request, call_next):
    job = request.headers.get("x-job-id")
    started = time.perf_counter()
    response: Response = await call_next(request)
    ms = (time.perf_counter() - started) * 1000
    if job:
        logger.info(
            "job_id=%s %s %s -> %s (%.1fms)",
            job,
            request.method,
            request.url.path,
            response.status_code,
            ms,
        )
    return response


@app.get("/health", tags=["ops"])
def health():
    return {
        "ok": True,
        "service": "computer-gateway",
        "workers": pool.worker_count,
        "leased": pool.leased_count(),
        "parked": pool.parked_count(),
        "free": pool.free_count(),
    }


@app.get("/ready", tags=["ops"])
def ready():
    """200 when at least one worker is free and a healthy worker exists."""
    free = pool.free_count()
    urls = [u for u, _ in _parse_workers()]
    healthy = any(pool.worker_healthy(u) for u in urls)
    accept_new = free > 0 and healthy
    body = {
        "ok": accept_new,
        "healthy_workers": healthy,
        "free": free,
        "leased": pool.leased_count(),
        "parked": pool.parked_count(),
    }
    if not accept_new:
        return JSONResponse(status_code=503, content=body)
    return body


@app.post("/v1/sessions", response_model=LeaseResponse, tags=["sessions"])
def create_session(
    _: AuthDep,
    body: LeaseRequest = LeaseRequest(),
    x_job_id: Annotated[str | None, Header()] = None,
):
    """Lease a free worker, or reattach a parked desktop when job_id matches."""
    job_id = body.job_id or x_job_id
    info = pool.lease(agent_id=body.agent_id, job_id=job_id, ttl_sec=body.ttl_sec)
    if not info:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "error": "no free workers",
                "free": pool.free_count(),
                "parked": pool.parked_count(),
            },
        )
    logger.info(
        "leased session=%s agent=%s job=%s worker=%s",
        info.session_id,
        info.agent_id,
        info.job_id,
        info.worker_base_url,
    )
    return _lease_response(info)


@app.post("/v1/sessions/{session_id}/heartbeat", response_model=LeaseResponse, tags=["sessions"])
def heartbeat(session_id: str, _: AuthDep, body: HeartbeatRequest = HeartbeatRequest()):
    info = pool.heartbeat(session_id, ttl_sec=body.ttl_sec)
    if not info:
        raise HTTPException(status_code=404, detail="session not found or expired")
    return _lease_response(info)


@app.post("/v1/sessions/{session_id}/acquire", response_model=LeaseResponse, tags=["sessions"])
def acquire_session(
    session_id: str,
    _: AuthDep,
    body: AcquireRequest = AcquireRequest(),
):
    """Re-lease a parked desktop by session_id (serial same-job leases)."""
    result = pool.acquire(
        session_id,
        ttl_sec=body.ttl_sec,
        agent_id=body.agent_id,
    )
    if result.error == "not_found":
        raise HTTPException(status_code=404, detail="session not found or expired")
    if result.error == "already_leased":
        raise HTTPException(status_code=409, detail="session already leased")
    assert result.info is not None
    logger.info(
        "acquired session=%s worker=%s",
        session_id,
        result.info.worker_base_url,
    )
    return _lease_response(result.info)


@app.delete("/v1/sessions/{session_id}", tags=["sessions"])
def delete_session(
    session_id: str,
    _: AuthDep,
    recycle: Annotated[bool, Query(description="Wipe Firefox and free the worker")] = False,
):
    """Soft release parks the warm desktop. Pass recycle=true to wipe and free."""
    if recycle:
        return _recycle_session(session_id)
    info = pool.release(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="session not found or expired")
    logger.info("parked session=%s worker=%s", session_id, info.worker_base_url)
    return {"ok": True, "session_id": session_id, "parked": True}


def _recycle_session(session_id: str) -> dict:
    worker_url = pool.recycle(session_id)
    if not worker_url:
        raise HTTPException(status_code=404, detail="session not found or expired")
    pool.reset_worker(worker_url)
    logger.info("recycled session=%s worker=%s", session_id, worker_url)
    return {"ok": True, "session_id": session_id, "recycled": True}


@app.post("/v1/sessions/{session_id}/recycle", tags=["sessions"])
def recycle_session(session_id: str, _: AuthDep):
    """Wipe Firefox and free the worker (from leased or parked)."""
    return _recycle_session(session_id)


@app.get("/v1/sessions/{session_id}/screenshot", tags=["observe"])
def screenshot(session_id: str, _: AuthDep):
    status, data = pool.proxy_get(session_id, "/screenshot")
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/sessions/{session_id}/action", tags=["act"])
def action(session_id: str, body: ActionBody, _: AuthDep):
    status, data = pool.proxy_post(
        session_id, "/action", body.model_dump(exclude_none=True)
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data


@app.post("/v1/sessions/{session_id}/reset", tags=["sessions"])
def reset_session(session_id: str, _: AuthDep):
    """Reset Firefox on the leased worker without releasing the lease."""
    status, data = pool.proxy_post(session_id, "/session/reset", {})
    if status >= 400:
        raise HTTPException(status_code=status, detail=data)
    return data
