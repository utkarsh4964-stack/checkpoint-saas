"""
Solari Sandbox adapter.

Two implementations behind one interface:

  - SolariSandboxRuntime: talks to the real Solari API via the actual
    published `solari-sandbox` PyPI package (confirmed: pip install
    solari-sandbox). The SDK is async — this adapter keeps a dedicated
    event loop thread so the live sandbox connection stays on one loop.

  - LocalFallbackRuntime: a plain local-directory stand-in so the whole
    Checkpoint flow (interception, risk, diff, rollback) can be built,
    tested, and demoed WITHOUT a live Solari key. Swap it out for the
    real runtime by setting SOLARI_API_KEY — see get_runtime() below.

Both implement the same tiny interface: boot(), run_command(), snapshot(),
restore(), teardown(). Nothing above this layer (checkpoint_manager,
risk engine, API) needs to know which one is active.
"""
from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from backend.config import settings
from typing import Optional


class SandboxRuntime(ABC):
    @abstractmethod
    def boot(self) -> str:
        """Start the sandbox, return a runtime handle."""

    def run_command(self, command: str, args: list[str] | None = None) -> dict:
        """Execute a shell-equivalent command inside the sandbox."""

    @abstractmethod
    def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    def delete_path(self, path: str) -> None: ...

    @abstractmethod
    def move_path(self, src: str, dst: str) -> None: ...

    @abstractmethod
    def make_dir(self, path: str) -> None: ...

    @abstractmethod
    def snapshot(self, note: str = "") -> str:
        """Take a snapshot, return a snapshot id."""

    @abstractmethod
    def restore(self, snapshot_id: str) -> None:
        """Restore the sandbox filesystem to a prior snapshot."""

    @abstractmethod
    def root_path(self) -> Path:
        """Local path to inspect for diffing (works for both impls)."""

    @abstractmethod
    def teardown(self) -> None: ...


class LocalFallbackRuntime(SandboxRuntime):
    """
    Local filesystem stand-in. Snapshots are just tarball copies under
    .checkpoint_snapshots/ — real "time travel", just not on Solari's
    microVMs. This is enough to build and demo the entire Checkpoint
    flow honestly; swap to SolariSandboxRuntime for the real submission.

    Cross-platform note: shell commands are run via subprocess with
    shell=True rather than hardcoding a Unix "sh" binary, since "sh"
    doesn't exist on stock Windows. See run_command() below.
    """

    def __init__(self, workdir: Optional[Path] = None):
        import tempfile
        base = Path(tempfile.gettempdir())
        self._workdir = workdir or base / f"checkpoint_sandbox_{uuid.uuid4().hex[:8]}"
        self._snapshot_dir = self._workdir.parent / f"{self._workdir.name}_snapshots"
        self._handle = f"local_{uuid.uuid4().hex[:8]}"

    def _safe_path(self, path: str) -> Path:
        raw = str(path)
        candidate = (self._workdir / raw).resolve()
        root = self._workdir.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"Path escapes the Checkpoint project root: {path}")
        return candidate

    def boot(self) -> str:
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        return self._handle

    def run_command(self, command: str, args: list[str] | None = None) -> dict:
        # Cross-platform: when called with our internal "run a shell
        # command" convention (command="sh", args=["-c", actual_cmd]),
        # execute the actual command through the native shell instead
        # of invoking a literal "sh" binary — "sh" doesn't exist on
        # stock Windows. subprocess's shell=True picks cmd.exe on
        # Windows and /bin/sh on Unix automatically.
        if command == "sh" and args and args[0] == "-c":
            actual_command = args[1]
            try:
                proc = subprocess.run(
                    actual_command, cwd=self._workdir, capture_output=True,
                    text=True, timeout=30, shell=True,
                )
                return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
            except subprocess.TimeoutExpired:
                return {"exit_code": 124, "stdout": "", "stderr": "command timed out"}

        # Direct binary invocation (e.g. "mkdir", ["-p", path]) — no
        # shell involved, argv passed straight to subprocess.
        full_args = [command] + (args or [])
        try:
            proc = subprocess.run(full_args, cwd=self._workdir, capture_output=True, text=True, timeout=30)
            return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        except FileNotFoundError as e:
            return {"exit_code": 127, "stdout": "", "stderr": str(e)}

    def write_file(self, path: str, content: str) -> None:
        full = self._safe_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def delete_path(self, path: str) -> None:
        full = self._safe_path(path)
        if full.is_dir():
            shutil.rmtree(full, ignore_errors=True)
        elif full.exists():
            full.unlink()

    def move_path(self, src: str, dst: str) -> None:
        full_src, full_dst = self._safe_path(src), self._safe_path(dst)
        full_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(full_src), str(full_dst))

    def make_dir(self, path: str) -> None:
        # Pure pathlib — no shelling out to "mkdir -p", which doesn't
        # exist as an invocable binary with that flag on Windows.
        self._safe_path(path).mkdir(parents=True, exist_ok=True)

    def snapshot(self, note: str = "") -> str:
        snap_id = f"snap_{uuid.uuid4().hex[:10]}"
        dest = self._snapshot_dir / snap_id
        shutil.copytree(self._workdir, dest)
        return snap_id

    def restore(self, snapshot_id: str) -> None:
        src = self._snapshot_dir / snapshot_id
        if not src.exists():
            raise ValueError(f"Unknown snapshot: {snapshot_id}")
        if self._workdir.exists():
            shutil.rmtree(self._workdir)
        shutil.copytree(src, self._workdir)

    def root_path(self) -> Path:
        return self._workdir

    def teardown(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)
        shutil.rmtree(self._snapshot_dir, ignore_errors=True)


class SolariSandboxRuntime(SandboxRuntime):
    """Real Solari-backed sandbox adapter.

    Solari's current Python SDK is async. Checkpoint's core is intentionally
    synchronous, so this adapter owns the async bridge. The remote sandbox is
    the source of truth; ``_mirror`` is a temporary local copy used only by
    Checkpoint's existing filesystem diff engine and the read-only agent tools.

    Current Solari APIs used here are documented as:
      - SandboxClient(api_key=..., base_url=...)
      - create(template=..., timeout_ms=...)
      - connect(), commands.run(), files.write/read_text, snapshot(name),
        revert(snapshot_id), kill()
    """

    BASE_URL = "https://api.getsolari.com"
    PROJECT_ROOT = "/project"

    def __init__(
        self,
        api_key: Optional[str] = None,
        template: str = "base",
        base_url: Optional[str] = None,
    ):
        self._api_key = (api_key or os.environ.get("SOLARI_API_KEY") or "").strip()
        if not self._api_key:
            raise ValueError("A Solari API key is required for SolariSandboxRuntime.")
        self._base_url = (base_url or os.environ.get("SOLARI_BASE_URL") or self.BASE_URL).rstrip("/")
        self._template = template
        self._sandbox = None
        self._client = None
        self._mirror = Path(__import__("tempfile").mkdtemp(prefix="checkpoint_solari_mirror_"))
        self._loop = None
        self._loop_thread = None

    def _ensure_loop(self):
        import asyncio
        import threading
        if self._loop is not None and self._loop.is_running():
            return
        ready = threading.Event()

        def runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()
            self._loop.close()

        self._loop_thread = threading.Thread(target=runner, name="checkpoint-solari-loop", daemon=True)
        self._loop_thread.start()
        ready.wait(timeout=5)
        if self._loop is None:
            raise RuntimeError("Failed to start Solari event loop")

    def _run_async(self, coro):
        import asyncio
        self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def _client_options(self):
        return {
            "api_key": self._api_key,
            "base_url": self._base_url,
        }

    def boot(self) -> str:
        from solari_sandbox import SandboxClient

        async def _boot():
            # Keep the client alive for the lifetime of the sandbox. The
            # current SDK cookbook uses timeout_ms on create; a generous idle
            # window keeps a live demo from being paused between actions.
            client = SandboxClient(**self._client_options())
            sb = await client.create(
                template=self._template,
                timeout_ms=settings.SOLARI_IDLE_TIMEOUT_MINUTES * 60 * 1000,
            )
            return client, sb

        self._client, self._sandbox = self._run_async(_boot())
        return getattr(self._sandbox, "sandboxId", getattr(self._sandbox, "id", "unknown"))

    def run_command(self, command: str, args: list[str] | None = None) -> dict:
        async def _run():
            return await self._sandbox.commands.run(
                command,
                args=args or [],
                cwd=self.PROJECT_ROOT,
            )
        result = self._run_async(_run())
        return {
            "exit_code": getattr(result, "exit_code", None),
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
        }

    def _remote_path(self, path: str) -> str:
        raw = str(path).replace("\\", "/")
        if raw.startswith(self.PROJECT_ROOT + "/") or raw == self.PROJECT_ROOT:
            candidate = posixpath.normpath(raw)
        else:
            candidate = posixpath.normpath(f"{self.PROJECT_ROOT}/{raw.lstrip('/')}")
        if candidate != self.PROJECT_ROOT and not candidate.startswith(self.PROJECT_ROOT + "/"):
            raise ValueError(f"Path escapes the Checkpoint project root: {path}")
        return candidate

    def write_file(self, path: str, content: str) -> None:
        remote = self._remote_path(path)
        parent = remote.rsplit("/", 1)[0]
        async def _write():
            await self._sandbox.commands.run("mkdir", args=["-p", parent])
            await self._sandbox.files.write(remote, content)
        self._run_async(_write())

    def delete_path(self, path: str) -> None:
        remote = self._remote_path(path)
        self.run_command("rm", ["-rf", remote])

    def move_path(self, src: str, dst: str) -> None:
        self.run_command("mv", [self._remote_path(src), self._remote_path(dst)])

    def make_dir(self, path: str) -> None:
        self.run_command("mkdir", ["-p", self._remote_path(path)])

    def snapshot(self, note: str = "") -> str:
        async def _snap():
            snap = await self._sandbox.snapshot(note or "checkpoint")
            return getattr(snap, "snapshotId", getattr(snap, "id", snap))
        return str(self._run_async(_snap()))

    def restore(self, snapshot_id: str) -> None:
        async def _restore():
            await self._sandbox.revert(snapshot_id)
        self._run_async(_restore())
        self._sync_mirror()

    def _sync_mirror(self) -> None:
        """Mirror /project into a local temp directory for filesystem diffs.

        The Solari SDK exposes guest files remotely rather than a host path.
        We therefore use the guest's Python interpreter to emit a compact JSON
        map of relative paths -> base64 content, then rebuild the local mirror.
        This mirror is never used for execution or rollback; Solari snapshots
        remain the source of truth for recovery.
        """
        import base64
        import json

        code = (
            "import os,json,base64; root='/project'; out={}; "
            "[(out.setdefault(os.path.relpath(p,root), base64.b64encode(open(os.path.join(root,p),'rb').read()).decode())) "
            "for p in [os.path.relpath(os.path.join(dp,f),root) for dp,ds,fs in os.walk(root) for f in fs]]; "
            "print(json.dumps(out,separators=(',',':')))"
        )
        result = self.run_command("python3", ["-c", code])
        if result["exit_code"] not in (0, None):
            raise RuntimeError(f"Failed to sync Solari filesystem: {result['stderr']}")
        try:
            data = json.loads(result["stdout"].strip() or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid filesystem sync response: {result['stdout'][:500]}") from exc

        if self._mirror.exists():
            for child in self._mirror.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        self._mirror.mkdir(parents=True, exist_ok=True)
        for rel, encoded in data.items():
            target = self._mirror / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded))

    def root_path(self) -> Path:
        self._sync_mirror()
        return self._mirror

    def teardown(self) -> None:
        try:
            if self._sandbox is not None and self._loop is not None:
                try:
                    self._run_async(self._sandbox.kill())
                except Exception:
                    pass
            if getattr(self, "_client", None) is not None and self._loop is not None:
                close = getattr(self._client, "close", None)
                if close:
                    try:
                        self._run_async(close())
                    except Exception:
                        pass
        finally:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread is not None and self._loop_thread.is_alive():
                self._loop_thread.join(timeout=2)
            self._loop = None
            self._loop_thread = None
            shutil.rmtree(self._mirror, ignore_errors=True)


def get_runtime(
    solari_api_key: Optional[str] = None,
    solari_base_url: Optional[str] = None,
) -> SandboxRuntime:
    """
    Select the runtime for one session.

    BYOK (Bring Your Own Key):
    - A caller may provide its own Solari API key per session.
    - The key is held only in the in-memory runtime object.
    - It is never written to SQLite, returned by the API, or logged.
    - If no per-session key is supplied, the server's SOLARI_API_KEY may
      be used for local/demo operation.
    - If neither exists, the safe local fallback is used.
    """
    key = (solari_api_key or settings.SOLARI_API_KEY or "").strip()
    if key:
        if settings.ENVIRONMENT == "production" and not solari_api_key:
            raise ValueError("A per-session Solari API key is required in production.")
        return SolariSandboxRuntime(
            api_key=key,
            base_url=solari_base_url or settings.SOLARI_BASE_URL,
        )
    if settings.ENVIRONMENT == "production" and not settings.ALLOW_LOCAL_FALLBACK:
        raise ValueError("A Solari API key is required in production.")
    return LocalFallbackRuntime()
