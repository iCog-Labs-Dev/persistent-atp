"""Access to a MORK space through its C FFI.

MORK exposes one entry point, `rust_mork(command, input) -> RustBuffer`. This
wraps the four commands `view.py` needs: add, remove, match, list.

Two facts about that boundary shape the code here.

The library will not load under a plain `CDLL` — it needs to be present at
process start, so `LD_PRELOAD` must name it (see `scripts/with-mork.sh`), and
`MORK_LIBRARY` or the `library_path` argument gives the path.

A malformed s-expression makes MORK panic, and `rust_mork` is `extern "C"`, so
the panic cannot unwind: the process aborts with nothing to catch. Every string
is checked for balance here, before it crosses, so a bad atom is a Python error
rather than a dead gate.

The Rust side holds one process-wide space behind a `OnceLock`; constructing a
second `MorkSpace` does not give you a second space.
"""

from __future__ import annotations

import ctypes
import os
from typing import Sequence

__all__ = ["MorkSpace", "MorkError", "MorkUnavailable", "MalformedAtom"]

_ENV_VAR = "MORK_LIBRARY"


class MorkError(Exception):
    """MORK answered a command with an error."""


class MorkUnavailable(MorkError):
    """The MORK library could not be located or loaded."""


class MalformedAtom(MorkError):
    """An s-expression was rejected before reaching MORK.

    Raised instead of letting MORK abort the process on a parse failure.
    """


class _RustBuffer(ctypes.Structure):
    """Mirrors `RustBuffer` in mork_ffi/src/lib.rs.

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

    def __init__(self, library_path: str | None = None):
        path = library_path or os.environ.get(_ENV_VAR)
        if not path:
            raise MorkUnavailable(
                f"set {_ENV_VAR} to libmork_ffi.so, and LD_PRELOAD to the same "
                "path so the library is loaded at process start"
            )
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
        buffer = self._call(command, payload)
        if not buffer.ptr:
            return ""
        reply = ctypes.string_at(buffer.ptr, buffer.len).decode("utf-8")
        if reply.startswith("ERR:"):
            raise MorkError(f"{command.decode()}: {reply[4:].strip()}")
        return reply

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
        return self._lines(self._command(b"match", query.encode("utf-8")))

    def atoms(self) -> list[str]:
        """Every atom in the space. Intended for tests and debugging."""
        return self._lines(self._command(b"get-atoms"))

    @staticmethod
    def _payload(sexprs: Sequence[str]) -> bytes:
        """Check each s-expression, then join them the way MORK reads them."""
        for sexpr in sexprs:
            check_balanced(sexpr)
        return "\n".join(sexpr.strip() for sexpr in sexprs).encode("utf-8")

    @staticmethod
    def _lines(reply: str) -> list[str]:
        return [line for line in reply.splitlines() if line.strip()]
