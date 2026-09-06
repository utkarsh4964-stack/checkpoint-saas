"""
Solari Sandbox adapter.

Two implementations behind one interface:

  - SolariSandboxRuntime: talks to the real Solari API via the published
    `solari-sandbox` package.
  - LocalFallbackRuntime: local-directory stand-in for offline development.

Both implement:
    boot()
    run_command()
    write_file()
    delete_path()
    move_path()
    make_dir()
    snapshot()
    restore()
    root_path()
    teardown()
"""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from backend.config import settings


class SandboxRuntime(ABC):
    """Small runtime interface used by CHECKPOINT."""

    @abstractmethod
    def boot(self) -> str:
        """Start the sandbox and return a runtime handle."""

    @abstractmethod
    def run_command(
        self,
        command: str,
        args: list[str] | None = None,
    ) -> dict:
        """Execute a command inside the sandbox."""

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write a file inside the protected workspace."""

    @abstractmethod
    def delete_path(self, path: str) -> None:
        """Delete a file or directory."""

    @abstractmethod
    def move_path(self, src: str, dst: str) -> None:
        """Move a file or directory."""

    @abstractmethod
    def make_dir(self, path: str) -> None:
        """Create a directory."""

    @abstractmethod
    def snapshot(self, note: str = "") -> str:
        """Create a recovery snapshot."""

    @abstractmethod
    def restore(self, snapshot_id: str) -> None:
        """Restore a recovery snapshot."""

    @abstractmethod
    def root_path(self) -> Path:
        """Return the local mirror used by CHECKPOINT's diff engine."""

    @abstractmethod
    def teardown(self) -> None:
        """Destroy/close the runtime."""


# ============================================================================
# LOCAL FALLBACK RUNTIME
# ============================================================================


class LocalFallbackRuntime(SandboxRuntime):
    """
    Local filesystem stand-in.

    Useful for:
      - local development
      - automated tests
      - offline demos

    It does NOT execute inside a microVM.
    """

    def __init__(self, workdir: Optional[Path] = None):
        import tempfile

        base = Path(tempfile.gettempdir())

        self._workdir = (
            workdir
            or base / f"checkpoint_sandbox_{uuid.uuid4().hex[:8]}"
        )

        self._snapshot_dir = (
            self._workdir.parent
            / f"{self._workdir.name}_snapshots"
        )

        self._handle = f"local_{uuid.uuid4().hex[:8]}"

    def _safe_path(self, path: str) -> Path:
        raw = str(path)

        candidate = (self._workdir / raw).resolve()
        root = self._workdir.resolve()

        if candidate != root and root not in candidate.parents:
            raise ValueError(
                f"Path escapes the Checkpoint project root: {path}"
            )

        return candidate

    def boot(self) -> str:
        self._workdir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._snapshot_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        return self._handle

    def run_command(
        self,
        command: str,
        args: list[str] | None = None,
    ) -> dict:

        # Internal shell convention:
        #
        #   command="sh"
        #   args=["-c", actual_command]
        #
        # Use the native shell so this also works on Windows.
        if command == "sh" and args and args[0] == "-c":
            actual_command = args[1]

            try:
                proc = subprocess.run(
                    actual_command,
                    cwd=self._workdir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    shell=True,
                )

                return {
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                }

            except subprocess.TimeoutExpired:
                return {
                    "exit_code": 124,
                    "stdout": "",
                    "stderr": "command timed out",
                }

        # Direct binary invocation.
        full_args = [command] + (args or [])

        try:
            proc = subprocess.run(
                full_args,
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=30,
            )

            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }

        except FileNotFoundError as exc:
            return {
                "exit_code": 127,
                "stdout": "",
                "stderr": str(exc),
            }

        except subprocess.TimeoutExpired:
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": "command timed out",
            }

    def write_file(
        self,
        path: str,
        content: str,
    ) -> None:

        full = self._safe_path(path)

        full.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        full.write_text(
            content,
            encoding="utf-8",
        )

    def delete_path(
        self,
        path: str,
    ) -> None:

        full = self._safe_path(path)

        if full.is_dir():
            shutil.rmtree(
                full,
                ignore_errors=True,
            )

        elif full.exists():
            full.unlink()

    def move_path(
        self,
        src: str,
        dst: str,
    ) -> None:

        full_src = self._safe_path(src)
        full_dst = self._safe_path(dst)

        full_dst.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.move(
            str(full_src),
            str(full_dst),
        )

    def make_dir(
        self,
        path: str,
    ) -> None:

        self._safe_path(path).mkdir(
            parents=True,
            exist_ok=True,
        )

    def snapshot(
        self,
        note: str = "",
    ) -> str:

        snap_id = f"snap_{uuid.uuid4().hex[:10]}"

        dest = self._snapshot_dir / snap_id

        shutil.copytree(
            self._workdir,
            dest,
        )

        return snap_id

    def restore(
        self,
        snapshot_id: str,
    ) -> None:

        src = self._snapshot_dir / snapshot_id

        if not src.exists():
            raise ValueError(
                f"Unknown snapshot: {snapshot_id}"
            )

        if self._workdir.exists():
            shutil.rmtree(
                self._workdir,
            )

        shutil.copytree(
            src,
            self._workdir,
        )

    def root_path(self) -> Path:
        return self._workdir

    def teardown(self) -> None:
        shutil.rmtree(
            self._workdir,
            ignore_errors=True,
        )

        shutil.rmtree(
            self._snapshot_dir,
            ignore_errors=True,
        )


# ============================================================================
# REAL SOLARI RUNTIME
# ============================================================================


class SolariSandboxRuntime(SandboxRuntime):
    """
    Real Solari-backed CHECKPOINT runtime.

    Solari is the source of truth for:
      - execution
      - snapshots
      - rollback

    CHECKPOINT keeps a temporary local mirror only for filesystem diffing.
    """

    BASE_URL = "https://api.getsolari.com"

    # This is the protected workspace inside the Solari sandbox.
    PROJECT_ROOT = "/project"

    def __init__(
        self,
        api_key: Optional[str] = None,
        template: str = "base",
        base_url: Optional[str] = None,
    ):

        self._api_key = (
            api_key
            or os.environ.get("SOLARI_API_KEY")
            or ""
        ).strip()

        if not self._api_key:
            raise ValueError(
                "A Solari API key is required for SolariSandboxRuntime."
            )

        self._base_url = (
            base_url
            or os.environ.get(
                "SOLARI_BASE_URL",
                self.BASE_URL,
            )
        ).rstrip("/")

        self._template = template

        self._sandbox = None
        self._client = None

        # Local mirror used ONLY by CHECKPOINT's diff engine.
        self._mirror = Path(
            __import__("tempfile").mkdtemp(
                prefix="checkpoint_solari_mirror_"
            )
        )

        self._loop = None
        self._loop_thread = None

    # ------------------------------------------------------------------
    # Async bridge
    # ------------------------------------------------------------------

    def _ensure_loop(self):
        import asyncio
        import threading

        if (
            self._loop is not None
            and self._loop.is_running()
        ):
            return

        ready = threading.Event()

        def runner():
            self._loop = asyncio.new_event_loop()

            asyncio.set_event_loop(
                self._loop
            )

            ready.set()

            self._loop.run_forever()

            self._loop.close()

        self._loop_thread = threading.Thread(
            target=runner,
            name="checkpoint-solari-loop",
            daemon=True,
        )

        self._loop_thread.start()

        ready.wait(timeout=5)

        if self._loop is None:
            raise RuntimeError(
                "Failed to start Solari event loop"
            )

    def _run_async(self, coro):
        import asyncio

        self._ensure_loop()

        future = asyncio.run_coroutine_threadsafe(
            coro,
            self._loop,
        )

        return future.result()

    # ------------------------------------------------------------------
    # Client configuration
    # ------------------------------------------------------------------

    def _client_options(self):
        return {
            "api_key": self._api_key,
            "base_url": self._base_url,
        }

    # ------------------------------------------------------------------
    # Sandbox lifecycle
    # ------------------------------------------------------------------

    def boot(self) -> str:
        from solari_sandbox import SandboxClient

        async def _boot():

            client = SandboxClient(
                **self._client_options()
            )

            sandbox = await client.create(
                template=self._template,
                timeout_ms=(
                    settings.SOLARI_IDLE_TIMEOUT_MINUTES
                    * 60
                    * 1000
                ),
            )

            return client, sandbox

        self._client, self._sandbox = (
            self._run_async(_boot())
        )

        # --------------------------------------------------------------
        # IMPORTANT FIX
        # --------------------------------------------------------------
        #
        # CHECKPOINT uses /project as the cwd for:
        #
        #   - root_path()
        #   - filesystem synchronization
        #   - shell actions
        #
        # The base Solari template does not necessarily create this
        # directory for us. Therefore create it immediately after
        # sandbox creation.
        #
        # This prevents the first action from failing before the actual
        # user command even executes.
        # --------------------------------------------------------------

        result = self.run_command(
            "mkdir",
            [
                "-p",
                self.PROJECT_ROOT,
            ],
        )

        if result.get("exit_code") not in (0, None):
            stderr = (
                result.get("stderr")
                or "command failed"
            )

            raise RuntimeError(
                "Failed to initialize Solari project root: "
                f"{stderr[:500]}"
            )

        return getattr(
            self._sandbox,
            "sandboxId",
            getattr(
                self._sandbox,
                "id",
                "unknown",
            ),
        )

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run_command(
        self,
        command: str,
        args: list[str] | None = None,
    ) -> dict:

        async def _run():

            return await self._sandbox.commands.run(
                command,
                args=args or [],
                cwd=self.PROJECT_ROOT,
            )

        result = self._run_async(
            _run()
        )

        return {
            "exit_code": getattr(
                result,
                "exit_code",
                None,
            ),
            "stdout": getattr(
                result,
                "stdout",
                "",
            ),
            "stderr": getattr(
                result,
                "stderr",
                "",
            ),
        }

    # ------------------------------------------------------------------
    # Path handling
    # ------------------------------------------------------------------

    def _remote_path(
        self,
        path: str,
    ) -> str:

        raw = str(path).replace(
            "\\",
            "/",
        )

        if (
            raw.startswith(
                self.PROJECT_ROOT + "/"
            )
            or raw == self.PROJECT_ROOT
        ):
            candidate = posixpath.normpath(
                raw
            )

        else:
            candidate = posixpath.normpath(
                f"{self.PROJECT_ROOT}/"
                f"{raw.lstrip('/')}"
            )

        if (
            candidate != self.PROJECT_ROOT
            and not candidate.startswith(
                self.PROJECT_ROOT + "/"
            )
        ):
            raise ValueError(
                "Path escapes the Checkpoint project root: "
                f"{path}"
            )

        return candidate

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def write_file(
        self,
        path: str,
        content: str,
    ) -> None:

        remote = self._remote_path(
            path
        )

        parent = remote.rsplit(
            "/",
            1,
        )[0]

        async def _write():

            await self._sandbox.commands.run(
                "mkdir",
                args=[
                    "-p",
                    parent,
                ],
                cwd=self.PROJECT_ROOT,
            )

            await self._sandbox.files.write(
                remote,
                content,
            )

        self._run_async(
            _write()
        )

    def delete_path(
        self,
        path: str,
    ) -> None:

        remote = self._remote_path(
            path
        )

        result = self.run_command(
            "rm",
            [
                "-rf",
                remote,
            ],
        )

        if result.get("exit_code") not in (
            0,
            None,
        ):
            raise RuntimeError(
                "Failed to delete path: "
                + (
                    result.get("stderr")
                    or "unknown error"
                )
            )

    def move_path(
        self,
        src: str,
        dst: str,
    ) -> None:

        result = self.run_command(
            "mv",
            [
                self._remote_path(src),
                self._remote_path(dst),
            ],
        )

        if result.get("exit_code") not in (
            0,
            None,
        ):
            raise RuntimeError(
                "Failed to move path: "
                + (
                    result.get("stderr")
                    or "unknown error"
                )
            )

    def make_dir(
        self,
        path: str,
    ) -> None:

        result = self.run_command(
            "mkdir",
            [
                "-p",
                self._remote_path(path),
            ],
        )

        if result.get("exit_code") not in (
            0,
            None,
        ):
            raise RuntimeError(
                "Failed to create directory: "
                + (
                    result.get("stderr")
                    or "unknown error"
                )
            )

    # ------------------------------------------------------------------
    # Solari snapshots
    # ------------------------------------------------------------------

    def snapshot(
        self,
        note: str = "",
    ) -> str:

        async def _snap():

            snap = await self._sandbox.snapshot(
                note or "checkpoint"
            )

            return getattr(
                snap,
                "snapshotId",
                getattr(
                    snap,
                    "id",
                    snap,
                ),
            )

        return str(
            self._run_async(
                _snap()
            )
        )

    def restore(
        self,
        snapshot_id: str,
    ) -> None:

        async def _restore():

            await self._sandbox.revert(
                snapshot_id
            )

        self._run_async(
            _restore()
        )

        # Keep CHECKPOINT's local diff mirror
        # consistent with the restored remote state.
        self._sync_mirror()

    # ------------------------------------------------------------------
    # Mirror synchronization
    # ------------------------------------------------------------------

    def _sync_mirror(self) -> None:
        """
        Copy the Solari /project filesystem into a temporary local mirror.

        The mirror is ONLY used for CHECKPOINT's diff engine.

        Execution and rollback remain entirely controlled by Solari.
        """

        import base64
        import json

        code = (
            "import os,json,base64; "
            "root='/project'; "
            "out={}; "
            "[(out.setdefault("
            "os.path.relpath(p,root),"
            "base64.b64encode("
            "open(os.path.join(root,p),'rb').read()"
            ").decode()"
            ")) "
            "for p in ["
            "os.path.relpath("
            "os.path.join(dp,f),"
            "root"
            ") "
            "for dp,ds,fs in os.walk(root) "
            "for f in fs"
            "]]; "
            "print("
            "json.dumps("
            "out,"
            "separators=(',',':')"
            ")"
            ")"
        )

        result = self.run_command(
            "python3",
            [
                "-c",
                code,
            ],
        )

        if result.get("exit_code") not in (
            0,
            None,
        ):
            raise RuntimeError(
                "Failed to sync Solari filesystem: "
                + (
                    result.get("stderr")
                    or "unknown error"
                )
            )

        stdout = (
            result.get("stdout")
            or ""
        ).strip()

        try:
            data = json.loads(
                stdout or "{}"
            )

        except json.JSONDecodeError as exc:

            raise RuntimeError(
                "Invalid filesystem sync response: "
                f"{stdout[:500]}"
            ) from exc

        # Clear the old mirror.
        if self._mirror.exists():

            for child in self._mirror.iterdir():

                if child.is_dir():

                    shutil.rmtree(
                        child
                    )

                else:

                    child.unlink()

        self._mirror.mkdir(
            parents=True,
            exist_ok=True,
        )

        # Rebuild mirror.
        for rel, encoded in data.items():

            target = (
                self._mirror
                / rel
            )

            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            target.write_bytes(
                base64.b64decode(
                    encoded
                )
            )

    def root_path(self) -> Path:
        self._sync_mirror()

        return self._mirror

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown(self) -> None:

        try:

            if (
                self._sandbox is not None
                and self._loop is not None
            ):

                try:

                    self._run_async(
                        self._sandbox.kill()
                    )

                except Exception:
                    pass

            if (
                getattr(
                    self,
                    "_client",
                    None,
                )
                is not None
                and self._loop is not None
            ):

                close = getattr(
                    self._client,
                    "close",
                    None,
                )

                if close:

                    try:

                        self._run_async(
                            close()
                        )

                    except Exception:
                        pass

        finally:

            if (
                self._loop is not None
                and self._loop.is_running()
            ):

                self._loop.call_soon_threadsafe(
                    self._loop.stop
                )

            if (
                self._loop_thread is not None
                and self._loop_thread.is_alive()
            ):

                self._loop_thread.join(
                    timeout=2
                )

            self._loop = None
            self._loop_thread = None

            shutil.rmtree(
                self._mirror,
                ignore_errors=True,
            )


# ============================================================================
# RUNTIME FACTORY
# ============================================================================


def get_runtime(
    solari_api_key: Optional[str] = None,
    solari_base_url: Optional[str] = None,
) -> SandboxRuntime:
    """
    Select the runtime for one CHECKPOINT session.

    Production:
        A Solari API key is required.

    Development:
        Local fallback can be used when explicitly enabled.
    """

    key = (
        solari_api_key
        or os.environ.get(
            "SOLARI_API_KEY"
        )
        or settings.SOLARI_API_KEY
    ).strip()

    base_url = (
        solari_base_url
        or os.environ.get(
            "SOLARI_BASE_URL"
        )
        or settings.SOLARI_BASE_URL
    ).strip()

    environment = (
        settings.ENVIRONMENT
        or "development"
    ).lower()

    # Production must use the real Solari runtime.
    if environment == "production":

        if not key:

            raise ValueError(
                "A Solari API key is required in production."
            )

        return SolariSandboxRuntime(
            api_key=key,
            base_url=base_url,
        )

    # Development with a key -> real Solari.
    if key:

        return SolariSandboxRuntime(
            api_key=key,
            base_url=base_url,
        )

    # Development fallback.
    if settings.ALLOW_LOCAL_FALLBACK:

        return LocalFallbackRuntime()

    raise ValueError(
        "No Solari API key is configured and local fallback is disabled."
    )
