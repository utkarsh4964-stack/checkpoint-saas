from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import get_current_user
from backend.core import registry
from backend.db import repositories as repo
from backend.errors import friendly_runtime_error
from backend.core.checkpoint_manager import SessionExpiredError
from backend.models.schemas import RollbackEvent, RollbackTrigger, User

router = APIRouter(prefix="/sessions", tags=["rollback"])


class RollbackRequest(BaseModel):
    reason: str = Field(default="Manual rollback requested", min_length=1, max_length=1000)


@router.post("/{session_id}/rollback/{checkpoint_id}", response_model=RollbackEvent)
def rollback_to_checkpoint(
    session_id: str, checkpoint_id: str, req: RollbackRequest, user: User = Depends(get_current_user)
) -> RollbackEvent:
    session = repo.get_session(session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "No active session/manager")
    checkpoint = repo.get_checkpoint(checkpoint_id)
    if not checkpoint or checkpoint.session_id != session_id:
        raise HTTPException(404, "Checkpoint not found")
    manager = registry.get(session_id)
    if not manager:
        raise HTTPException(404, "No active session/manager")
    try:
        return manager.rollback(checkpoint_id, reason=req.reason, trigger=RollbackTrigger.MANUAL)
    except (KeyError,) as exc:
        raise HTTPException(404, "Checkpoint not found") from exc
    except SessionExpiredError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, friendly_runtime_error(exc)) from exc
