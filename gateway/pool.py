"""Exclusive session leases over a fixed set of worker desktops."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WorkerSlot:
    base_url: str
    vnc_url: str
    session_id: str | None = None
    agent_id: str | None = None
    job_id: str | None = None
    expires_at: datetime | None = None


@dataclass
class LeaseInfo:
    session_id: str
    worker_base_url: str
    vnc_url: str
    expires_at: datetime
    agent_id: str | None = None
    job_id: str | None = None


class SessionPool:
    def __init__(
        self,
        workers: list[tuple[str, str]],
        *,
        default_ttl_sec: int = 600,
        http_timeout: float = 30.0,
    ) -> None:
        if not workers:
            raise ValueError("at least one worker is required")
        self._default_ttl = default_ttl_sec
        self._timeout = http_timeout
        self._lock = threading.Lock()
        self._slots: list[WorkerSlot] = [
            WorkerSlot(base_url=u.rstrip("/"), vnc_url=v) for u, v in workers
        ]

    @property
    def worker_count(self) -> int:
        return len(self._slots)

    def _expire_locked(self, now: datetime) -> list[WorkerSlot]:
        expired: list[WorkerSlot] = []
        for slot in self._slots:
            if (
                slot.session_id
                and slot.expires_at
                and slot.expires_at <= now
            ):
                expired.append(slot)
                slot.session_id = None
                slot.agent_id = None
                slot.job_id = None
                slot.expires_at = None
        return expired

    def sweep_expired(self) -> list[str]:
        """Clear expired leases; return worker URLs that should be reset."""
        with self._lock:
            expired = self._expire_locked(_utcnow())
            return [s.base_url for s in expired]

    def free_count(self) -> int:
        with self._lock:
            self._expire_locked(_utcnow())
            return sum(1 for s in self._slots if s.session_id is None)

    def leased_count(self) -> int:
        with self._lock:
            self._expire_locked(_utcnow())
            return sum(1 for s in self._slots if s.session_id is not None)

    def lease(
        self,
        *,
        agent_id: str | None = None,
        job_id: str | None = None,
        ttl_sec: int | None = None,
    ) -> LeaseInfo | None:
        ttl = ttl_sec if ttl_sec is not None else self._default_ttl
        if ttl < 30:
            ttl = 30
        now = _utcnow()
        with self._lock:
            self._expire_locked(now)
            for slot in self._slots:
                if slot.session_id is not None:
                    continue
                sid = str(uuid.uuid4())
                expires = now + timedelta(seconds=ttl)
                slot.session_id = sid
                slot.agent_id = agent_id
                slot.job_id = job_id
                slot.expires_at = expires
                return LeaseInfo(
                    session_id=sid,
                    worker_base_url=slot.base_url,
                    vnc_url=slot.vnc_url,
                    expires_at=expires,
                    agent_id=agent_id,
                    job_id=job_id,
                )
        return None

    def heartbeat(self, session_id: str, ttl_sec: int | None = None) -> LeaseInfo | None:
        ttl = ttl_sec if ttl_sec is not None else self._default_ttl
        if ttl < 30:
            ttl = 30
        now = _utcnow()
        with self._lock:
            self._expire_locked(now)
            slot = self._find_locked(session_id)
            if not slot:
                return None
            slot.expires_at = now + timedelta(seconds=ttl)
            return LeaseInfo(
                session_id=session_id,
                worker_base_url=slot.base_url,
                vnc_url=slot.vnc_url,
                expires_at=slot.expires_at,
                agent_id=slot.agent_id,
                job_id=slot.job_id,
            )

    def release(self, session_id: str) -> str | None:
        """Release lease; return worker base_url to reset, or None if unknown."""
        with self._lock:
            slot = self._find_locked(session_id)
            if not slot:
                return None
            url = slot.base_url
            slot.session_id = None
            slot.agent_id = None
            slot.job_id = None
            slot.expires_at = None
            return url

    def worker_for(self, session_id: str) -> WorkerSlot | None:
        with self._lock:
            self._expire_locked(_utcnow())
            return self._find_locked(session_id)

    def _find_locked(self, session_id: str) -> WorkerSlot | None:
        for slot in self._slots:
            if slot.session_id == session_id:
                return slot
        return None

    def proxy_get(self, session_id: str, path: str) -> tuple[int, Any]:
        slot = self.worker_for(session_id)
        if not slot:
            return 404, {"detail": "session not found or expired"}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(f"{slot.base_url}{path}")
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def proxy_post(self, session_id: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        slot = self.worker_for(session_id)
        if not slot:
            return 404, {"detail": "session not found or expired"}
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(f"{slot.base_url}{path}", json=body or {})
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text}
            return r.status_code, data

    def reset_worker(self, worker_base_url: str) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            try:
                client.post(f"{worker_base_url}/session/reset")
            except httpx.HTTPError:
                pass

    def worker_healthy(self, worker_base_url: str) -> bool:
        with httpx.Client(timeout=5.0) as client:
            try:
                r = client.get(f"{worker_base_url}/health")
                if r.status_code != 200:
                    return False
                data = r.json()
                return bool(data.get("ok", True))
            except httpx.HTTPError:
                return False
