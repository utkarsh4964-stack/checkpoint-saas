"""
Core data models for Checkpoint.

These are Pydantic schemas used across the API, the risk engine, and the
SQLite repositories. Keep this file dependency-light — no DB or Solari
imports here, so it can be imported from anywhere without cycles.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class RuntimeType(str, Enum):
    SOLARI_SANDBOX = "solari_sandbox"
    SOLARI_BROWSER = "solari_browser"  # P1, not implemented in v0.2.1
    SOLARI_DESKTOP = "solari_desktop"  # P2, not implemented


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"  # auto-ended by the idle/lifetime cleanup sweep


class ActionType(str, Enum):
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    FILE_DELETE = "file.delete"
    FILE_MOVE = "file.move"
    DIR_CREATE = "dir.create"
    SHELL_EXECUTE = "shell.execute"


class RiskTier(str, Enum):
    SAFE = "safe"            # 0-30
    SUSPICIOUS = "suspicious"  # 31-70
    DANGEROUS = "dangerous"    # 71-100


class ActionStatus(str, Enum):
    PENDING = "pending"        # created, not yet risk-checked
    ALLOWED = "allowed"        # executed, no approval needed
    PAUSED = "paused"          # awaiting human approval
    APPROVED = "approved"      # human approved after pause
    REJECTED = "rejected"      # human rejected, action not applied
    BLOCKED = "blocked"        # auto-blocked pre-execution, never ran
    COMPLETED = "completed"    # executed and diff/risk finalized
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"          # execution/runtime failure; action did not finalize
    RECOVERY_FAILED = "recovery_failed"  # rollback was requested but could not be completed


class RollbackTrigger(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------

class RiskFinding(BaseModel):
    id: str = Field(default_factory=lambda: _id("risk"))
    action_id: str
    rule: str
    severity: int  # points this rule contributed, 0-100
    message: str
    confidence: float = 1.0


class RiskResult(BaseModel):
    score: int  # 0-100, normalized
    tier: RiskTier
    findings: list[RiskFinding]

    @property
    def requires_approval(self) -> bool:
        return self.tier == RiskTier.DANGEROUS

    @property
    def should_log_only(self) -> bool:
        return self.tier == RiskTier.SUSPICIOUS


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------

class FileDiffEntry(BaseModel):
    path: str
    change: str  # "added" | "removed" | "modified"
    before_preview: Optional[str] = None
    after_preview: Optional[str] = None


class DiffResult(BaseModel):
    files_added: list[str] = Field(default_factory=list)
    files_removed: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    entries: list[FileDiffEntry] = Field(default_factory=list)

    @property
    def total_files_touched(self) -> int:
        return len(self.files_added) + len(self.files_removed) + len(self.files_modified)


# --------------------------------------------------------------------------
# Session / Checkpoint / Action
# --------------------------------------------------------------------------

class User(BaseModel):
    id: str = Field(default_factory=lambda: _id("user"))
    email: str
    password_hash: str
    created_at: datetime = Field(default_factory=_now)
    is_active: bool = True


class Session(BaseModel):
    id: str = Field(default_factory=lambda: _id("sess"))
    user_id: str
    agent_id: str
    runtime: RuntimeType
    status: SessionStatus = SessionStatus.RUNNING
    started_at: datetime = Field(default_factory=_now)
    ended_at: Optional[datetime] = None
    # Bumped on every action/timeline touch; the cleanup sweep uses this
    # (plus started_at) to expire idle or overlong sessions.
    last_active_at: datetime = Field(default_factory=_now)
    # Solari sandbox handle for this session, set once the runtime boots.
    runtime_handle: Optional[str] = None


class Checkpoint(BaseModel):
    id: str = Field(default_factory=lambda: _id("chk"))
    session_id: str
    sequence: int
    snapshot_id: str  # Solari snapshot ref, or "none" for a skipped checkpoint
    created_at: datetime = Field(default_factory=_now)
    note: Optional[str] = None


class AgentAction(BaseModel):
    """
    An action the agent wants to take. `intent` is the agent's own
    natural-language justification, captured BEFORE execution — this is
    what the risk engine compares against `type` + `target` + `parameters`
    to detect intent/actual-action mismatches.
    """
    id: str = Field(default_factory=lambda: _id("act"))
    session_id: str
    checkpoint_id: Optional[str] = None
    type: ActionType
    intent: str
    target: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    reversible: bool = True

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Action intent cannot be empty.")
        if len(value) > 4000:
            raise ValueError("Action intent is too long.")
        return value

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 1000:
            raise ValueError("Action target is too long.")
        return value
    status: ActionStatus = ActionStatus.PENDING
    risk_score: int = 0
    started_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None
    diff: Optional[DiffResult] = None


class RollbackEvent(BaseModel):
    id: str = Field(default_factory=lambda: _id("rb"))
    session_id: str
    checkpoint_id: str
    trigger: RollbackTrigger
    reason: str
    created_at: datetime = Field(default_factory=_now)


class TimelineEntry(BaseModel):
    """Flattened view of one action for the timeline UI/API response."""
    action: AgentAction
    findings: list[RiskFinding] = Field(default_factory=list)
