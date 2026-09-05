"""
Background session-lifecycle sweep.

Runs on a timer inside the single Uvicorn worker (see main.py). For
each session still marked RUNNING in the DB:

  - if a live CheckpointManager is registered for it, and it has gone
    idle past SESSION_IDLE_TIMEOUT_MINUTES or lived past
    SESSION_MAX_LIFETIME_MINUTES, tear down its Solari sandbox and
    mark the session EXPIRED.
  - if no live manager is registered (e.g. the process restarted and
    the in-memory registry was lost), the DB row is still marked
    RUNNING with no way to reconnect the runtime — mark it EXPIRED
    directly so the UI shows an honest "runtime expired" state
    instead of a session that looks alive but isn't.

This is what makes "server restart -> orphaned running sessions"
recoverable without manual DB surgery.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from backend.config import settings
from backend.core import registry
from backend.db import repositories as repo
from backend.models.schemas import SessionStatus
from backend.ratelimit import rate_limiter

logger = logging.getLogger("checkpoint.cleanup")


def _is_expired(session, now: datetime) -> bool:
    last_active = session.last_active_at or session.started_at
    idle_for = now - last_active
    alive_for = now - session.started_at
    return (
        idle_for > timedelta(minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES)
        or alive_for > timedelta(minutes=settings.SESSION_MAX_LIFETIME_MINUTES)
    )


def sweep_once() -> int:
    now = datetime.now(timezone.utc)
    expired_count = 0
    rate_limiter.sweep()
    for session in repo.list_running_sessions():
        if not _is_expired(session, now):
            continue
        manager = registry.get(session.id)
        try:
            if manager is not None:
                manager.end_session(status_ok=True, status_override=SessionStatus.EXPIRED)
            else:
                # No live runtime to tear down (process restarted) — the
                # DB row is the only truth left; reflect reality in it.
                session.status = SessionStatus.EXPIRED
                session.ended_at = now
                session.last_active_at = now
                repo.update_session(session)
        except Exception:
            logger.exception("Failed to clean up expired session %s", session.id)
        finally:
            registry.remove(session.id)
            expired_count += 1
    return expired_count


async def cleanup_loop() -> None:
    while True:
        try:
            n = await asyncio.to_thread(sweep_once)
            if n:
                logger.info("Cleanup sweep expired %d session(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session cleanup sweep failed")
        await asyncio.sleep(settings.CLEANUP_INTERVAL_SECONDS)
