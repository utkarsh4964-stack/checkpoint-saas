"""
Agent tools: the ONLY way an LLM agent can touch the sandbox.

Per priority #2's hard requirement — "the LLM must NEVER directly
control the runtime" — every tool function here does exactly one
thing: build an intent + action, hand it to CheckpointManager.submit_action(),
and return whatever Checkpoint decided (allowed/blocked/paused, plus the
diff and risk findings) as the tool result. The LLM never gets a raw
shell or filesystem handle.

TOOL_SCHEMAS is OpenAI/Groq-compatible function-calling JSON — the same
shape both APIs expect in the `tools=[...]` parameter, so this works
against either provider's chat.completions.create() unchanged.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.checkpoint_manager import CheckpointManager
from backend.models.schemas import ActionStatus, ActionType

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in the sandbox, optionally under a subpath.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Subpath to list, default '.'"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content. Every call is intercepted by Checkpoint's risk engine before and after it runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "intent": {"type": "string", "description": "Why you are writing this file, in your own words."},
                },
                "required": ["path", "content", "intent"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file or directory. Intercepted by Checkpoint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                    "intent": {"type": "string"},
                },
                "required": ["source", "destination", "intent"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file or directory. This is a high-risk action — Checkpoint may block it before it runs if it matches a secret-file or sensitive-path pattern, or pause it after running if the diff reveals unexpected scale.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "intent": {"type": "string", "description": "State plainly what you intend to remove and why — Checkpoint compares this against what actually happens."},
                },
                "required": ["path", "intent"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Run a shell command in the sandbox (via `sh -c`). Highest-risk tool — every destructive keyword, secret-file target, or bulk change is checked by Checkpoint before and after execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The full shell command, e.g. \"rm -rf reports\""},
                    "intent": {"type": "string"},
                },
                "required": ["command", "intent"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolExecutor:
    """
    Executes a tool call by name, always through CheckpointManager.
    Returns a plain dict suitable for feeding back to the LLM as a tool
    result message — includes status, risk_score, and a human-readable
    summary the model can react to (e.g. "BLOCKED: ...").
    """

    def __init__(self, manager: CheckpointManager):
        self.manager = manager

    def call(self, name: str, arguments: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return handler(**arguments)
        except TypeError as exc:
            # LLM providers occasionally emit an extra argument despite a
            # strict tool schema. Do not crash the whole autonomous run;
            # return the validation error to the model so it can retry with
            # the exact schema. We intentionally do not silently discard
            # arguments because that can hide a materially different action.
            return {
                "error": f"Invalid arguments for {name}: {exc}",
                "result": "Tool call rejected before execution. Retry using only the declared arguments.",
            }
        except Exception as exc:
            return {
                "error": f"Tool {name} failed: {type(exc).__name__}: {exc}",
                "result": "Tool execution failed safely; no unhandled exception escaped the agent loop.",
            }

    # ------------------------------------------------------------------
    # Read-only tools: no Checkpoint interception needed (nothing to
    # risk-check or roll back), but still routed through the same
    # runtime so the LLM never gets a raw filesystem handle.
    # ------------------------------------------------------------------

    def _safe_local_path(self, path: str) -> tuple[Path, Path]:
        if self.manager.runtime is None:
            raise RuntimeError("Session runtime is unavailable.")
        root = self.manager.runtime.root_path().resolve()
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("Path is outside the sandbox project directory.")
        return root, candidate

    def _tool_list_files(self, path: str = ".") -> dict:
        root, target = self._safe_local_path(path)
        if not target.exists():
            return {"error": f"Path does not exist: {path}"}
        entries = []
        for item in target.rglob("*"):
            entries.append(item.relative_to(root).as_posix() + ("/" if item.is_dir() else ""))
            if len(entries) >= 1000:
                return {"path": path, "entries": sorted(entries), "truncated": True}
        return {"path": path, "entries": sorted(entries), "truncated": False}

    def _tool_read_file(self, path: str) -> dict:
        root, full = self._safe_local_path(path)
        if not full.exists():
            return {"error": f"File does not exist: {path}"}
        try:
            if not full.is_file():
                return {"error": f"Not a file: {path}"}
            return {"path": path, "content": full.read_text(errors="replace")[:2000]}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Mutating tools: EVERY one of these goes through
    # CheckpointManager.submit_action(), which runs the full pre-check
    # -> execute -> diff -> post-check flow. This is the enforcement
    # point — there is no path from an LLM tool call to the runtime
    # that skips Checkpoint.
    # ------------------------------------------------------------------

    def _tool_write_file(self, path: str, content: str, intent: str) -> dict:
        action = self.manager.submit_action(
            type=ActionType.FILE_WRITE, intent=intent, target=path, parameters={"content": content},
        )
        return self._summarize(action)

    def _tool_move_file(self, source: str, destination: str, intent: str) -> dict:
        action = self.manager.submit_action(
            type=ActionType.FILE_MOVE, intent=intent, target=source, parameters={"destination": destination},
        )
        return self._summarize(action)

    def _tool_delete_file(self, path: str, intent: str) -> dict:
        action = self.manager.submit_action(type=ActionType.FILE_DELETE, intent=intent, target=path)
        return self._summarize(action)

    def _tool_execute_command(self, command: str, intent: str) -> dict:
        action = self.manager.submit_action(
            type=ActionType.SHELL_EXECUTE, intent=intent, target=None, parameters={"command": command},
        )
        return self._summarize(action)

    def _summarize(self, action) -> dict:
        from backend.db import repositories as repo
        findings = repo.list_findings(action.id)
        result = {
            "action_id": action.id,
            "status": action.status.value,
            "risk_score": action.risk_score,
            "findings": [f"{f.rule}: {f.message}" for f in findings],
        }
        if action.status == ActionStatus.BLOCKED:
            result["result"] = (
                f"BLOCKED before execution (risk {action.risk_score}/100). "
                f"This action never touched the sandbox. Do not retry it as-is — "
                f"reconsider your approach given the findings above."
            )
        elif action.status == ActionStatus.PAUSED:
            result["result"] = (
                f"Action executed but is now PAUSED pending human approval "
                f"(risk {action.risk_score}/100 detected from the actual diff). "
                f"A human will approve or reject it; you cannot force this through."
            )
        else:
            result["result"] = f"Completed normally (risk {action.risk_score}/100)."
        return result
