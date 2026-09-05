from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
import threading
from pydantic import BaseModel, Field, SecretStr, field_validator

from backend.auth import get_current_user
from backend.config import settings
from backend.core import registry
from backend.core.checkpoint_manager import CheckpointManager
from backend.db import repositories as repo
from backend.errors import friendly_runtime_error
from backend.models.schemas import Session, User

router = APIRouter(prefix="/sessions", tags=["sessions"])
_session_start_lock = threading.Lock()


class StartSessionRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    task_description: str = Field(default="", max_length=settings.MAX_ACTION_INTENT_LENGTH)
    # Bring Your Own Solari Key. SecretStr prevents accidental repr/log leakage.
    solari_api_key: SecretStr | None = None
    solari_base_url: str | None = Field(default=None, max_length=500)

    @field_validator("solari_base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("Solari base URL must use HTTPS.")
        configured = settings.SOLARI_BASE_URL.rstrip("/")
        if value != configured:
            raise ValueError("Custom Solari endpoints are disabled; use the configured Solari API endpoint.")
        return value


def _owned_session(session_id: str, user: User) -> Session:
    """Fetch a session and enforce tenant isolation. 404s (not 403s) on
    someone else's session, so we don't confirm the ID exists at all."""
    session = repo.get_session(session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "Session not found")
    return session


@router.get("", response_model=list[Session])
def list_my_sessions(user: User = Depends(get_current_user)) -> list[Session]:
    return repo.list_sessions_for_user(user.id)


@router.post("", response_model=Session)
def start_session(req: StartSessionRequest, user: User = Depends(get_current_user)) -> Session:
    # Serialize the quota check + runtime creation so concurrent requests
    # cannot both observe an available slot and exceed the per-user limit.
    with _session_start_lock:
        active = repo.count_active_sessions(
            user.id,
            idle_timeout_minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES,
            max_lifetime_minutes=settings.SESSION_MAX_LIFETIME_MINUTES,
        )
        if active >= settings.MAX_ACTIVE_SESSIONS_PER_USER:
            raise HTTPException(
                429,
                f"You already have {active} active session(s) — the limit is "
                f"{settings.MAX_ACTIVE_SESSIONS_PER_USER}. End one before starting another.",
            )

        solari_key = req.solari_api_key.get_secret_value() if req.solari_api_key else None
        if not solari_key and settings.ENVIRONMENT == "production" and not settings.SOLARI_API_KEY:
            raise HTTPException(400, "A Solari API key is required to start a production session.")

        manager = CheckpointManager(
            task_description=req.task_description,
            solari_api_key=solari_key,
            solari_base_url=req.solari_base_url,
        )
        try:
            session = manager.start_session(agent_id=req.agent_id, user_id=user.id)
        except Exception as exc:
            raise HTTPException(502, friendly_runtime_error(exc)) from exc
        registry.register(session.id, manager)
        return session


@router.get("/{session_id}", response_model=Session)
def get_session(session_id: str, user: User = Depends(get_current_user)) -> Session:
    return _owned_session(session_id, user)


@router.post("/{session_id}/end")
def end_session(session_id: str, status_ok: bool = True, user: User = Depends(get_current_user)):
    _owned_session(session_id, user)
    manager = registry.get(session_id)
    if not manager:
        raise HTTPException(404, "No active runtime for this session (already ended or the server restarted)")
    try:
        manager.end_session(status_ok=status_ok)
    finally:
        registry.remove(session_id)
    return {"status": "ended"}


@router.get("/{session_id}/timeline")
def get_timeline(session_id: str, user: User = Depends(get_current_user)):
    _owned_session(session_id, user)
    actions = repo.list_actions(session_id)
    checkpoints = repo.list_checkpoints(session_id)
    rollbacks = repo.list_rollback_events(session_id)
    entries = []
    for action in actions:
        entries.append({
            "action": action.model_dump(),
            "findings": [f.model_dump() for f in repo.list_findings(action.id)],
        })
    return {
        "session_id": session_id,
        "status": _owned_session(session_id, user).status.value,
        "actions": entries,
        "checkpoints": [c.model_dump() for c in checkpoints],
        "rollback_events": [r.model_dump() for r in rollbacks],
        "summary": {
            "total_actions": len(actions),
            "total_checkpoints": len(checkpoints),
            "blocked_actions": sum(1 for a in actions if a.status.value == "blocked"),
            "rollbacks": len(rollbacks),
        },
        "limits": {
            "max_actions_per_session": settings.MAX_ACTIONS_PER_SESSION,
            "actions_used": len(actions),
        },
    }
