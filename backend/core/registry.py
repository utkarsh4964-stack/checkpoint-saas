"""
In-process registry mapping session_id -> live CheckpointManager.

A CheckpointManager holds a live runtime handle (e.g. an open local
sandbox directory, or a connected Solari sandbox) that can't be fully
reconstructed from the DB row alone. For a single-process demo this
in-memory map is enough; a real multi-instance deployment would need
to persist/reconnect runtime handles instead.
"""
from __future__ import annotations

from backend.core.checkpoint_manager import CheckpointManager

_managers: dict[str, CheckpointManager] = {}


def register(session_id: str, manager: CheckpointManager) -> None:
    _managers[session_id] = manager


def get(session_id: str) -> CheckpointManager | None:
    return _managers.get(session_id)


def remove(session_id: str) -> None:
    _managers.pop(session_id, None)
