"""Filesystem diff engine.

The diff is intentionally scoped to the sandbox project filesystem. File
hashing is streamed in chunks so a large file cannot consume unbounded host
memory during a checkpoint comparison.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from backend.models.schemas import DiffResult, FileDiffEntry

MAX_PREVIEW_CHARS = 300
HASH_CHUNK_SIZE = 1024 * 1024
MAX_DIFF_FILES = 10_000


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_tree(root: Path) -> dict[str, str]:
    """Return {relative_path: sha256(content)} for files under root."""
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if path.is_file():
            if len(result) >= MAX_DIFF_FILES:
                raise RuntimeError(f"Filesystem contains more than {MAX_DIFF_FILES} files; diff refused.")
            result[path.relative_to(root).as_posix()] = _sha256_file(path)
    return result


def _preview(root: Path, rel_path: str) -> str | None:
    full = root / rel_path
    try:
        raw = full.read_bytes()[: MAX_PREVIEW_CHARS * 4]
        return raw.decode("utf-8", errors="replace")[:MAX_PREVIEW_CHARS]
    except Exception:
        return None


def diff_trees(before: dict[str, str], after: dict[str, str], root_after: Path) -> DiffResult:
    before_paths = set(before)
    after_paths = set(after)

    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    modified = sorted(p for p in (before_paths & after_paths) if before[p] != after[p])

    total = len(added) + len(removed) + len(modified)
    if total > MAX_DIFF_FILES:
        raise RuntimeError(f"Diff contains more than {MAX_DIFF_FILES} changed files; review refused.")

    entries: list[FileDiffEntry] = []
    for path in added:
        entries.append(FileDiffEntry(path=path, change="added", after_preview=_preview(root_after, path)))
    for path in removed:
        entries.append(FileDiffEntry(path=path, change="removed"))
    for path in modified:
        entries.append(FileDiffEntry(path=path, change="modified", after_preview=_preview(root_after, path)))

    return DiffResult(files_added=added, files_removed=removed, files_modified=modified, entries=entries)
