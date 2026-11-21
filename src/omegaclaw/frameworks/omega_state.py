"""Serializable, nonauthoritative workflow cache; never an admission ledger."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
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
        self._memory = self._empty()

    def _empty(self):
        return {"version": 1, "scope": self.scope, "intents": {}, "proofs": {}, "selected_proof": None}

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
                if data.get("version") != 1 or data.get("scope") != self.scope:
                    raise ValueError("workflow cache belongs to a different receiver/workspace")
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
