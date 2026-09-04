"""Content-addressed artifact storage seam for reports, data snapshots, and model outputs."""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import tempfile

from .contracts import ContractError


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, expected_hash: str | None = None) -> str:
        digest = hashlib.sha256(content).hexdigest()
        if expected_hash and digest != expected_hash:
            raise ContractError("artifact content does not match expected hash")
        target = self.root / digest
        if target.exists() and target.read_bytes() != content:
            raise ContractError("content-addressed artifact collision detected")
        if not target.exists():
            # Publish only a complete fsynced object; never overwrite a winner
            # when concurrent workers produce the same content.
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if target.read_bytes() != content:
                        raise ContractError("content-addressed artifact collision detected")
            finally:
                temporary.unlink()
        return digest

    def get(self, digest: str) -> bytes:
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ContractError("invalid artifact digest")
        content = (self.root / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ContractError("stored artifact failed content verification")
        return content
