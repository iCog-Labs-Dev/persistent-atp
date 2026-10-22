"""Explicit reference byte store implementing ArtifactStore's byte interface."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from artifacts.hashing import hash_content, is_valid_hash


class ReferenceArtifactStore:
    mode = "reference-byte-fake"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        pointer = hash_content(content)
        path = self.root / pointer.split(":", 1)[1]
        if not path.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if path.read_bytes() != content:
            raise ValueError("artifact hash collision or corruption")
        return pointer

    def get(self, pointer: str) -> bytes:
        if not is_valid_hash(pointer):
            raise ValueError("invalid artifact pointer")
        content = (self.root / pointer.split(":", 1)[1]).read_bytes()
        if hash_content(content) != pointer:
            raise ValueError("artifact bytes corrupted")
        return content

    def exists(self, pointer: str) -> bool:
        try:
            self.get(pointer)
            return True
        except (FileNotFoundError, ValueError):
            return False
