"""
Checkpoint Python SDK — deliberately narrow (per spec: don't build a
universal framework-agnostic SDK for this product).

This talks to the Checkpoint backend over HTTP so an agent process can
be fully decoupled from the backend's internals. For the demo you can
also skip HTTP and use CheckpointManager directly in-process — see
examples/workspace_agent.py for both patterns.

Desired developer experience (from the spec):

    from checkpoint import Checkpoint

    checkpoint = Checkpoint(api_key=CHECKPOINT_KEY, solari_api_key=SOLARI_KEY)
    session = checkpoint.start_session(agent_id="workspace-agent", runtime="solari_sandbox")
    result = await session.run(intent="Clean temporary files", action=agent_action)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class ActionResult:
    id: str
    status: str
    risk_score: int
    raw: dict[str, Any]


class CheckpointSession:
    def __init__(self, client: "Checkpoint", session_id: str):
        self._client = client
        self.session_id = session_id

    def run(self, intent: str, type: str, target: str | None = None,
            parameters: dict | None = None) -> ActionResult:
        """
        Submit one action through Checkpoint. Blocks until the action
        completes (or is blocked/paused) — no separate poll step needed
        for the MVP's synchronous flow.
        """
        resp = self._client._http.post(
            f"/sessions/{self.session_id}/actions",
            json={
                "session_id": self.session_id,
                "type": type,
                "intent": intent,
                "target": target,
                "parameters": parameters or {},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return ActionResult(id=data["id"], status=data["status"], risk_score=data["risk_score"], raw=data)

    def approve(self, action_id: str) -> ActionResult:
        resp = self._client._http.post(f"/actions/{action_id}/approve")
        resp.raise_for_status()
        data = resp.json()
        return ActionResult(id=data["id"], status=data["status"], risk_score=data["risk_score"], raw=data)

    def reject(self, action_id: str, rollback: bool = True) -> ActionResult:
        resp = self._client._http.post(f"/actions/{action_id}/reject", params={"rollback": rollback})
        resp.raise_for_status()
        data = resp.json()
        return ActionResult(id=data["id"], status=data["status"], risk_score=data["risk_score"], raw=data)

    def timeline(self) -> dict:
        resp = self._client._http.get(f"/sessions/{self.session_id}/timeline")
        resp.raise_for_status()
        return resp.json()

    def end(self, status_ok: bool = True) -> None:
        resp = self._client._http.post(f"/sessions/{self.session_id}/end", params={"status_ok": status_ok})
        resp.raise_for_status()


class Checkpoint:
    """Entry point for the SDK. Points at a running Checkpoint backend.

    v0.2 requires a Checkpoint account: pass an existing `access_token`,
    or call `login()`/`register()` once to obtain one. Every request
    after that carries the token as a Bearer header, and the server
    only ever returns sessions owned by that account.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: str = "http://localhost:8000",
        solari_api_key: Optional[str] = None,
        solari_base_url: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.api_key = api_key  # reserved for a future Checkpoint API-key flow
        self._solari_api_key = solari_api_key
        self._solari_base_url = solari_base_url
        self.access_token = access_token
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        if self.access_token:
            self._http.headers["Authorization"] = f"Bearer {self.access_token}"

    def register(self, email: str, password: str) -> str:
        resp = self._http.post("/auth/register", json={"email": email, "password": password})
        resp.raise_for_status()
        return self._store_token(resp.json())

    def login(self, email: str, password: str) -> str:
        resp = self._http.post("/auth/login", json={"email": email, "password": password})
        resp.raise_for_status()
        return self._store_token(resp.json())

    def _store_token(self, data: dict) -> str:
        self.access_token = data["access_token"]
        self._http.headers["Authorization"] = f"Bearer {self.access_token}"
        return self.access_token

    def start_session(self, agent_id: str, runtime: str = "solari_sandbox",
                       task_description: str = "") -> CheckpointSession:
        payload = {
            "agent_id": agent_id,
            "task_description": task_description,
        }
        # BYOK: forward the caller's Solari key only for session creation.
        # The Checkpoint server does not persist or return it.
        if self._solari_api_key:
            payload["solari_api_key"] = self._solari_api_key
        if self._solari_base_url:
            payload["solari_base_url"] = self._solari_base_url

        resp = self._http.post("/sessions", json=payload)
        resp.raise_for_status()
        session_id = resp.json()["id"]
        return CheckpointSession(self, session_id)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
