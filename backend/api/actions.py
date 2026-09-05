from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import get_current_user
from backend.config import settings
from backend.core import registry
from backend.db import repositories as repo
from backend.errors import friendly_runtime_error
from backend.core.checkpoint_manager import ActionQuotaExceeded, InvalidActionState, SessionExpiredError
from backend.models.schemas import ActionType, AgentAction, User

router = APIRouter(tags=["actions"])


class SubmitActionRequest(BaseModel):
    session_id: str
    type: ActionType
    intent: str
    target: str | None = None
    parameters: dict = Field(default_factory=dict)


def _owned_session_or_404(session_id: str, user: User):
    session = repo.get_session(session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "No active session/manager")
    return session


def _owned_action_or_404(action_id: str, user: User) -> AgentAction:
    action = repo.get_action(action_id)
    if not action:
        raise HTTPException(404, "Action not found")
    session = repo.get_session(action.session_id)
    if not session or session.user_id != user.id:
        raise HTTPException(404, "Action not found")
    return action


@router.post("/sessions/{session_id}/actions", response_model=AgentAction)
def submit_action(session_id: str, req: SubmitActionRequest, user: User = Depends(get_current_user)) -> AgentAction:
    _owned_session_or_404(session_id, user)
    if req.session_id != session_id:
        raise HTTPException(400, "The action session_id must match the URL session.")
    manager = registry.get(session_id)
    if not manager:
        raise HTTPException(404, "No active session/manager")

    used = repo.count_actions(session_id)
    if used >= settings.MAX_ACTIONS_PER_SESSION:
        raise HTTPException(
            429,
            f"This session has reached its limit of {settings.MAX_ACTIONS_PER_SESSION} actions. "
            "Start a new session to continue.",
        )

    try:
        return manager.submit_action(
            type=req.type, intent=req.intent, target=req.target, parameters=req.parameters
        )
    except ActionQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except SessionExpiredError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, friendly_runtime_error(exc)) from exc


@router.get("/actions/{action_id}", response_model=AgentAction)
def get_action(action_id: str, user: User = Depends(get_current_user)) -> AgentAction:
    return _owned_action_or_404(action_id, user)


@router.get("/actions/{action_id}/risk")
def get_action_risk(action_id: str, user: User = Depends(get_current_user)):
    action = _owned_action_or_404(action_id, user)
    findings = repo.list_findings(action_id)
    return {
        "action_id": action_id,
        "risk_score": action.risk_score,
        "status": action.status,
        "findings": [f.model_dump() for f in findings],
    }


@router.post("/actions/{action_id}/approve", response_model=AgentAction)
def approve_action(action_id: str, user: User = Depends(get_current_user)) -> AgentAction:
    action = _owned_action_or_404(action_id, user)
    manager = registry.get(action.session_id)
    if not manager:
        raise HTTPException(404, "No active session/manager")
    try:
        return manager.approve_action(action_id)
    except (InvalidActionState, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except SessionExpiredError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, friendly_runtime_error(exc)) from exc


@router.post("/actions/{action_id}/reject", response_model=AgentAction)
def reject_action(action_id: str, rollback: bool = True, user: User = Depends(get_current_user)) -> AgentAction:
    action = _owned_action_or_404(action_id, user)
    manager = registry.get(action.session_id)
    if not manager:
        raise HTTPException(404, "No active session/manager")
    try:
        return manager.reject_action(action_id, rollback=rollback)
    except (InvalidActionState, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except SessionExpiredError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, friendly_runtime_error(exc)) from exc
