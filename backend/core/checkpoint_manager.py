"""
Checkpoint orchestration core.

The manager is deliberately stateful: one live manager owns one sandbox
runtime. A per-session lock serializes actions so two concurrent HTTP
requests cannot take overlapping checkpoints or mutate the same sandbox
at the same time.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from backend.config import settings
from backend.core import diff_engine
from backend.db import repositories as repo
from backend.models.schemas import (
    ActionStatus,
    ActionType,
    AgentAction,
    Checkpoint,
    RiskResult,
    RiskTier,
    RollbackEvent,
    RollbackTrigger,
    RuntimeType,
    Session,
    SessionStatus,
)
from backend.risk import engine as risk_engine
from backend.runtimes.solari_sandbox import SandboxRuntime, get_runtime

NO_SNAPSHOT_TYPES = {ActionType.FILE_READ}


class ActionBlocked(Exception):
    def __init__(self, risk: RiskResult):
        self.risk = risk
        super().__init__(f"Action blocked, risk={risk.score}")


class ActionQuotaExceeded(Exception):
    pass


class SessionExpiredError(Exception):
    pass


class InvalidActionState(Exception):
    pass


class CheckpointManager:
    def __init__(self, task_description: str = "", solari_api_key: str | None = None,
                 solari_base_url: str | None = None):
        self.task_description = task_description
        self._solari_api_key = solari_api_key
        self._solari_base_url = solari_base_url
        self.runtime: SandboxRuntime | None = None
        self.session: Session | None = None
        self._lock = threading.RLock()

    def _remove_self_from_registry(self) -> None:
        # Lazy import avoids a module cycle at import time.
        try:
            from backend.core import registry
            if self.session:
                registry.remove(self.session.id)
        except Exception:
            pass

    def _ensure_active(self) -> None:
        if self.session is None or self.runtime is None:
            raise SessionExpiredError("This Checkpoint session is no longer active.")
        if self.session.status != SessionStatus.RUNNING:
            raise SessionExpiredError("This Checkpoint session is no longer active.")

        now = datetime.now(timezone.utc)
        last_active = self.session.last_active_at or self.session.started_at
        idle_expired = now - last_active > timedelta(minutes=settings.SESSION_IDLE_TIMEOUT_MINUTES)
        lifetime_expired = now - self.session.started_at > timedelta(minutes=settings.SESSION_MAX_LIFETIME_MINUTES)
        if idle_expired or lifetime_expired:
            self.end_session(status_override=SessionStatus.EXPIRED)
            self._remove_self_from_registry()
            raise SessionExpiredError("This Checkpoint session expired. Start a new session to continue.")

    def _touch(self) -> None:
        assert self.session is not None
        now = datetime.now(timezone.utc)
        self.session.last_active_at = now
        repo.touch_session_activity(self.session.id, now)

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start_session(self, agent_id: str, user_id: str) -> Session:
        with self._lock:
            self.runtime = get_runtime(
                solari_api_key=self._solari_api_key,
                solari_base_url=self._solari_base_url,
            )
            try:
                handle = self.runtime.boot()
                session = Session(
                    user_id=user_id,
                    agent_id=agent_id,
                    runtime=RuntimeType.SOLARI_SANDBOX,
                    runtime_handle=handle,
                )
                repo.create_session(session)
                self.session = session
                # Initial checkpoint makes rollback-to-start possible.
                self._checkpoint(note="Initial state")
                return session
            except Exception:
                if self.session is not None:
                    try:
                        self.session.status = SessionStatus.FAILED
                        self.session.ended_at = datetime.now(timezone.utc)
                        self.session.last_active_at = self.session.ended_at
                        repo.update_session(self.session)
                    except Exception:
                        pass
                if self.runtime is not None:
                    try:
                        self.runtime.teardown()
                    except Exception:
                        pass
                self.runtime = None
                raise

    def end_session(self, status_ok: bool = True, status_override=None) -> None:
        with self._lock:
            if self.session is None:
                return
            if self.session.status != SessionStatus.RUNNING:
                return
            status = status_override or (SessionStatus.COMPLETED if status_ok else SessionStatus.FAILED)
            now = datetime.now(timezone.utc)
            self.session.status = status
            self.session.ended_at = now
            self.session.last_active_at = now
            repo.update_session(self.session)
            runtime = self.runtime
            self.runtime = None
            if runtime:
                try:
                    runtime.teardown()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    def _checkpoint(self, note: str = "") -> Checkpoint:
        assert self.session is not None and self.runtime is not None
        snap_id = self.runtime.snapshot(note=note)
        seq = repo.next_checkpoint_sequence(self.session.id)
        cp = Checkpoint(session_id=self.session.id, sequence=seq, snapshot_id=snap_id, note=note)
        return repo.create_checkpoint(cp)

    # ------------------------------------------------------------------
    # Main action flow
    # ------------------------------------------------------------------

    def submit_action(self, type: ActionType, intent: str, target: str | None = None,
                      parameters: dict | None = None) -> AgentAction:
        with self._lock:
            self._ensure_active()
            assert self.session is not None and self.runtime is not None

            if len(intent.strip()) > settings.MAX_ACTION_INTENT_LENGTH:
                raise ValueError("Action intent is too long.")
            if target is not None and len(target) > settings.MAX_TARGET_LENGTH:
                raise ValueError("Action target is too long.")
            if type in {ActionType.FILE_WRITE, ActionType.FILE_DELETE, ActionType.FILE_MOVE, ActionType.DIR_CREATE} and not target:
                raise ValueError("A target path is required for this action type.")
            if type == ActionType.FILE_MOVE and not (parameters or {}).get("destination"):
                raise ValueError("A destination is required for file.move actions.")
            if type == ActionType.SHELL_EXECUTE and not str((parameters or {}).get("command", "")).strip():
                raise ValueError("A shell command is required.")
            if type == ActionType.SHELL_EXECUTE and len(str((parameters or {}).get("command", ""))) > settings.MAX_COMMAND_LENGTH:
                raise ValueError("Shell command is too long.")
            if type == ActionType.FILE_WRITE and len(str((parameters or {}).get("content", ""))) > settings.MAX_FILE_CONTENT_LENGTH:
                raise ValueError("File content exceeds the configured size limit.")

            if repo.count_actions(self.session.id) >= settings.MAX_ACTIONS_PER_SESSION:
                raise ActionQuotaExceeded(
                    f"This session has reached its limit of {settings.MAX_ACTIONS_PER_SESSION} actions."
                )

            action = AgentAction(
                session_id=self.session.id,
                type=type,
                intent=intent,
                target=target,
                parameters=parameters or {},
            )
            repo.create_action(action)
            self._touch()

            # 1. Pre-execution: dangerous actions are never sent to runtime.
            pre_risk = risk_engine.assess_pre_execution(action, self.task_description)
            repo.create_findings(pre_risk.findings)
            if pre_risk.tier == RiskTier.DANGEROUS:
                action.status = ActionStatus.BLOCKED
                action.risk_score = pre_risk.score
                action.completed_at = _now()
                repo.update_action(action)
                return action

            # 2. Checkpoint before any potentially mutating action.
            try:
                if type not in NO_SNAPSHOT_TYPES:
                    checkpoint = self._checkpoint(note=f"Before: {intent}")
                    action.checkpoint_id = checkpoint.id
                    repo.update_action(action)
            except Exception:
                action.status = ActionStatus.FAILED
                action.completed_at = _now()
                repo.update_action(action)
                raise

            # 3. Execute + diff. A runtime failure is recorded as FAILED;
            # it is never presented as a successfully completed action.
            try:
                before_root = self.runtime.root_path()
                before_tree = diff_engine.snapshot_tree(before_root)
                self._execute(action)
                after_root = self.runtime.root_path()
                after_tree = diff_engine.snapshot_tree(after_root)
                action.diff = diff_engine.diff_trees(before_tree, after_tree, after_root)
            except Exception as exc:
                action.status = ActionStatus.FAILED
                action.completed_at = _now()
                repo.update_action(action)
                raise exc

            # 4. Post-execution: diff-aware risk assessment.
            post_risk = risk_engine.assess_post_execution(
                action, action.diff, self.task_description
            )
            action.risk_score = max(pre_risk.score, post_risk.score)
            existing_rules = {f.rule for f in pre_risk.findings}
            repo.create_findings([f for f in post_risk.findings if f.rule not in existing_rules])

            action.status = (
                ActionStatus.PAUSED
                if post_risk.tier == RiskTier.DANGEROUS
                else ActionStatus.COMPLETED
            )
            action.completed_at = _now()
            self._touch()
            repo.update_action(action)
            return action

    def _execute(self, action: AgentAction) -> None:
        assert self.runtime is not None
        if action.type == ActionType.FILE_WRITE:
            self.runtime.write_file(action.target, action.parameters.get("content", ""))
        elif action.type == ActionType.FILE_DELETE:
            self.runtime.delete_path(action.target)
        elif action.type == ActionType.FILE_MOVE:
            self.runtime.move_path(action.target, action.parameters["destination"])
        elif action.type == ActionType.DIR_CREATE:
            self.runtime.make_dir(action.target)
        elif action.type == ActionType.SHELL_EXECUTE:
            command = action.parameters.get("command", "")
            result = self.runtime.run_command("sh", ["-c", command])
            if result.get("exit_code") not in (0, None):
                stderr = result.get("stderr") or "command failed"
                raise RuntimeError(f"Sandbox command failed: {stderr[:500]}")
        elif action.type == ActionType.FILE_READ:
            return
        else:
            raise ValueError(f"Unsupported action type: {action.type}")

    # ------------------------------------------------------------------
    # Human decision path
    # ------------------------------------------------------------------

    def _get_owned_session_action(self, action_id: str) -> AgentAction:
        assert self.session is not None
        action = repo.get_action(action_id)
        if action is None or action.session_id != self.session.id:
            raise KeyError("Action not found")
        return action

    def approve_action(self, action_id: str) -> AgentAction:
        with self._lock:
            self._ensure_active()
            action = self._get_owned_session_action(action_id)
            if action.status != ActionStatus.PAUSED:
                raise InvalidActionState("Only paused actions can be approved.")
            action.status = ActionStatus.APPROVED
            repo.update_action(action)
            self._touch()
            return action

    def reject_action(self, action_id: str, rollback: bool = True) -> AgentAction:
        with self._lock:
            self._ensure_active()
            action = self._get_owned_session_action(action_id)
            if action.status != ActionStatus.PAUSED:
                raise InvalidActionState("Only paused actions can be rejected.")
            if rollback and action.checkpoint_id:
                try:
                    self.rollback(
                        action.checkpoint_id,
                        reason=f"Rejected action {action.id}",
                        trigger=RollbackTrigger.MANUAL,
                    )
                except Exception:
                    action.status = ActionStatus.RECOVERY_FAILED
                    repo.update_action(action)
                    raise
            action.status = ActionStatus.REJECTED
            repo.update_action(action)
            self._touch()
            return action

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self, checkpoint_id: str, reason: str,
                 trigger: RollbackTrigger = RollbackTrigger.MANUAL) -> RollbackEvent:
        with self._lock:
            self._ensure_active()
            assert self.session is not None and self.runtime is not None
            checkpoint = repo.get_checkpoint(checkpoint_id)
            if checkpoint is None or checkpoint.session_id != self.session.id:
                raise KeyError("Checkpoint not found")
            self.runtime.restore(checkpoint.snapshot_id)
            event = RollbackEvent(
                session_id=self.session.id,
                checkpoint_id=checkpoint_id,
                trigger=trigger,
                reason=reason,
            )
            repo.create_rollback_event(event)
            self._touch()
            return event


def _now() -> datetime:
    return datetime.now(timezone.utc)
