from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import repositories as repo
from backend.models.schemas import Checkpoint, User

router = APIRouter(prefix="/sessions", tags=["checkpoints"])


def _require_owned_session(session_id: str, user: User):
    session = repo.get_session(session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "Session not found")
    return session


@router.get("/{session_id}/checkpoints", response_model=list[Checkpoint])
def list_checkpoints(session_id: str, user: User = Depends(get_current_user)) -> list[Checkpoint]:
    _require_owned_session(session_id, user)
    return repo.list_checkpoints(session_id)


@router.get("/checkpoints/{checkpoint_id}", response_model=Checkpoint)
def get_checkpoint(checkpoint_id: str, user: User = Depends(get_current_user)) -> Checkpoint:
    cp = repo.get_checkpoint(checkpoint_id)
    if not cp:
        raise HTTPException(404, "Checkpoint not found")
    session = repo.get_session(cp.session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "Checkpoint not found")
    return cp
