"""Access to a MORK space through its C FFI.

MORK exposes one entry point, `rust_mork(command, input) -> RustBuffer`. This
wraps graph reads/writes plus fixed-dependency backup, restore and build identity.

Two facts about that boundary shape the code here.

The library will not load under a plain `CDLL` — it needs to be present at
process start, so `LD_PRELOAD` must name it (see `scripts/with-mork.sh`), and
`MORK_LIBRARY` or the `library_path` argument gives the path.

The fork catches unwinding panics and returns typed error replies. Python
balance checks reject malformed expressions before crossing; an unrecoverable
native panic invalidates the shared Space until host restart and journal replay.

The Rust side holds one process-wide space behind a `OnceLock`; constructing a
second `MorkSpace` does not give you a second space.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
from collections import OrderedDict
from typing import Sequence

__all__ = ["MorkSpace", "MorkError", "MorkUnavailable", "MalformedAtom"]

_ENV_VAR = "MORK_LIBRARY"


class MorkError(Exception):
    """MORK answered a command with an error."""


class MorkUnavailable(MorkError):
    """The MORK library could not be located or loaded."""


class MalformedAtom(MorkError):
    """An s-expression was rejected before reaching MORK.

    Raised before crossing the native parser.
    """


class _RustBuffer(ctypes.Structure):
    """Mirrors `RustBuffer` in MORK/ffi/src/lib.rs.

    Exactly two fields. A third one makes ctypes read past the struct and
    segfault.
    """

    _fields_ = [("ptr", ctypes.c_void_p), ("len", ctypes.c_size_t)]


def check_balanced(sexpr: str) -> None:
    """Raise `MalformedAtom` unless `sexpr` is one balanced s-expression.

    Parens inside a quoted string do not count, and a backslash escapes the next
    character. A guard against aborting the process, not a MORK parser: it
    accepts strings MORK may still reject for other reasons.
    """
    text = sexpr.strip()
    if not text:
        raise MalformedAtom("empty s-expression")
    if not text.startswith("("):
        raise MalformedAtom(f"s-expression must start with '(': {sexpr!r}")

    depth = 0
    in_string = False
    escaped = False
    for position, character in enumerate(text):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif in_string:
            if character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and position != len(text) - 1:
                raise MalformedAtom(
                    f"trailing text after the s-expression in {sexpr!r}: "
                    f"{text[position + 1:]!r}"
                )
            if depth < 0:
                raise MalformedAtom(f"unbalanced ')' in {sexpr!r}")

    if in_string:
        raise MalformedAtom(f"unterminated string in {sexpr!r}")
    if depth != 0:
        raise MalformedAtom(f"{depth} unclosed '(' in {sexpr!r}")


class MorkSpace:
    """The process-wide MORK space.

    Every method takes and returns s-expression text; building it is the
    caller's job. See `mork.backend.view` for the atom shapes this project uses.
    """

    # The cdylib owns one Space, so wrappers and views must share coordination.
    lock = threading.RLock()
    invalid_proofs: set[str] = set()
    recovery = threading.local()
    recovery_targets: dict[str, int] = {}
    unavailable = False
    # Derived, bounded materialization only. Canonical mutations invalidate
    # affected-proof results across wrappers; restore/uncertain input resets all.
    closure_cache: OrderedDict[str, tuple[str, ...]] = OrderedDict()
    dependency_indexes: OrderedDict = OrderedDict()
    dependency_unknown: set[str] | None = set()

    def __init__(self, library_path: str | None = None):
        path = library_path or os.environ.get(_ENV_VAR)
        if not path:
            raise MorkUnavailable(
                f"set {_ENV_VAR} to libmork_ffi.so, and LD_PRELOAD to the same "
                "path so the library is loaded at process start"
            )
        self.library_path = os.path.realpath(path)
        try:
            library = ctypes.CDLL(path)
        except OSError as exc:
            hint = ""
            if "static TLS" in str(exc):
                hint = f"; preload it instead: LD_PRELOAD={path}"
            raise MorkUnavailable(f"could not load {path!r}: {exc}{hint}") from exc

        try:
            self._call = library.rust_mork
        except AttributeError as exc:
            raise MorkUnavailable(
                f"{path!r} exports no rust_mork; is it the Prolog wrapper "
                "(morklib.so) rather than the Rust cdylib?"
            ) from exc
        self._call.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self._call.restype = _RustBuffer

    def _command(self, command: bytes, payload: bytes = b"") -> str:
        """Run one FFI command and decode its reply."""
        if b"\0" in command or b"\0" in payload:
            raise MorkError("embedded NUL in FFI request")
        from mork.dependencies import prepare, finish
        with self.lock:
            changes = prepare(self, command, payload)
            succeeded = False
            try:
                buffer = self._call(command, payload)
                if not buffer.ptr:
                    succeeded = True
                    return ""
                try:
                    reply = ctypes.string_at(buffer.ptr, buffer.len).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise MorkError("non-UTF-8 FFI reply") from exc
                if reply.startswith("ERR:"):
                    if "internal panic" in reply or "space poisoned" in reply:
                        type(self).unavailable = True
                    raise MorkError(f"{command.decode()}: {reply[4:].strip()}")
                succeeded = True
                return reply
            finally:
                finish(self, changes, command, succeeded)

    def build_info(self) -> dict:
        """Build identity exported by the loaded fork."""
        try:
            return json.loads(self._command(b"get-build-info"))
        except (ValueError, TypeError) as exc:
            raise MorkError("invalid build info reply") from exc

    def add(self, *sexprs: str) -> None:
        """Add atoms. Adding one that is already present changes nothing."""
        self._command(b"add-atoms", self._payload(sexprs))

    def remove(self, *sexprs: str) -> None:
        """Remove atoms, matched by exact bytes.

        Returns nothing because MORK reports success whether or not anything
        matched: never treat a completed call as evidence an atom was there. Read
        it back with `match` first and remove what you were given.
        """
        self._command(b"remove-atoms", self._payload(sexprs))

    def match(self, pattern: str, template: str) -> list[str]:
        """Every `template` filled in from one binding of `pattern`.

        `pattern` may contain `$name` variables and `template` may use the same
        names; a bare `$v` template yields the raw value, quotes included. Empty
        when nothing matches.
        """
        check_balanced(pattern)
        query = f"({pattern} {template})"
        check_balanced(query)
        return self._lines(self._command(b"match", query.encode("utf-8")))

    def atoms(self) -> list[str]:
        """Every atom in the space. Intended for tests and debugging."""
        return self._lines(self._command(b"get-atoms"))

    def backup(self, directory: str) -> None:
        """Snapshot the whole space into `directory` (created if needed).

        The kernel validates the restored tree before exposing it on the
        matching `restore` (see mork/snapshot.py for the bundle format).
        """
        self._command(b"backup", directory.encode("utf-8"))

    def restore(self, directory: str) -> None:
        """Replace the space with the validated snapshot in `directory`."""
        self._command(b"restore", directory.encode("utf-8"))

    @staticmethod
    def _payload(sexprs: Sequence[str]) -> bytes:
        """Check each s-expression, then join them the way MORK reads them."""
        for sexpr in sexprs:
            check_balanced(sexpr)
        return "\n".join(sexpr.strip() for sexpr in sexprs).encode("utf-8")

    @staticmethod
    def _lines(reply: str) -> list[str]:
        return [line for line in reply.splitlines() if line.strip()]
