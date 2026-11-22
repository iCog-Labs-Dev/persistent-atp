"""Serializable, nonauthoritative workflow cache; never an admission ledger."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading


class OmegaState:
    def __init__(self, path, *, scope):
        self.path = Path(path) if path is not None else None
        self.scope = scope
        self._lock = threading.RLock()
        self._proof_locks = {}
        self._coordinating = threading.local()
        self._memory = self._empty()

    def _empty(self):
        return {"version": 2, "scope": self.scope, "intents": {}, "proofs": {},
                "selected_proof": None, "tasks": {}}

    @contextmanager
    def _access(self, write=False):
        with self._lock:
            if self.path is None:
                yield self._memory
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
                os.chmod(lock.name, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                data = json.loads(self.path.read_text()) if self.path.exists() else self._empty()
                if data.get("version") not in {1, 2} or data.get("scope") != self.scope:
                    raise ValueError("workflow cache belongs to a different receiver/workspace")
                # Add operational task records without changing prepared requests,
                # admission references, cursors or notification acknowledgments.
                data.setdefault("tasks", {})
                data["version"] = 2
                yield data
                if write:
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
                            temporary = stream.name
                            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, self.path)
                        directory = os.open(self.path.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    finally:
                        if temporary and os.path.exists(temporary):
                            os.unlink(temporary)

    def snapshot(self):
        with self._access() as data:
            return deepcopy(data)

    @contextmanager
    def coordinate_proof(self, proof):
        """Serialize proof handoff and dispatch, including across cache reopen.

        These locks protect coordinator decisions, not mathematical authority.
        Separate proof files keep slow provider reads from blocking siblings.
        """
        held = getattr(self._coordinating, 'proofs', set())
        if proof in held:
            yield True
            return
        with self._lock:
            lock = self._proof_locks.setdefault(proof, threading.Lock())
        if not lock.acquire(blocking=False):
            yield False
            return
        stream = None
        try:
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(proof.encode()).hexdigest()
                stream = self.path.with_suffix(self.path.suffix + ".proof-" + digest + ".lock").open("a")
                os.chmod(stream.name, 0o600)
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    yield False
                    return
            self._coordinating.proofs = held | {proof}
            try:
                yield True
            finally:
                self._coordinating.proofs = held
        finally:
            if stream is not None:
                stream.close()
            lock.release()

    def update(self, function):
        with self._access(write=True) as data:
            return deepcopy(function(data))

    def intent(self, identity):
        return self.snapshot()["intents"][identity]

    def patch(self, identity, **values):
        def update(data):
            data["intents"][identity].update(values)
            return data["intents"][identity]
        return self.update(update)
