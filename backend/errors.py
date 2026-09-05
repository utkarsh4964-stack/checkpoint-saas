"""
Translate low-level Solari/runtime exceptions into messages a user can
act on, instead of a bare 500. Nothing here changes behavior — it's a
presentation layer wrapped around calls into CheckpointManager /
SandboxRuntime.
"""
from __future__ import annotations


def friendly_runtime_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "api key" in msg or "unauthorized" in msg or "401" in msg or "forbidden" in msg or "403" in msg:
        return "We couldn't create your Solari sandbox — check your API key and try again."
    if "timeout" in msg or "timed out" in msg:
        return "Your Solari sandbox timed out while starting. Please try again in a moment."
    if "connect" in msg or "connection" in msg or "network" in msg:
        return "We couldn't reach Solari right now. Please try again shortly."
    if "quota" in msg or "rate limit" in msg or "429" in msg or "too many" in msg:
        return "Your Solari account has hit a usage limit. Check your Solari plan and try again."
    if "escapes the checkpoint project root" in msg:
        return "That path is outside the sandbox's project directory and was rejected."
    return "We couldn't complete this Solari sandbox operation. Please check your API key and try again."
