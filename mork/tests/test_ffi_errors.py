"""Python boundary failures must not silently corrupt or truncate requests."""
import ctypes
from unittest.mock import Mock

import pytest

from mork.backend.ffi import MorkSpace, MorkError, MalformedAtom, _RustBuffer


def test_non_utf8_reply_is_a_mork_error():
    payload = ctypes.create_string_buffer(b'\xff')
    space = MorkSpace.__new__(MorkSpace)
    space._call = lambda *_: _RustBuffer(ctypes.addressof(payload), 1)
    with pytest.raises(MorkError, match='non-UTF-8'):
        space._command(b'match')


def test_nul_and_malformed_template_are_rejected_before_native_call():
    space = MorkSpace.__new__(MorkSpace)
    space._call = Mock()
    with pytest.raises(MorkError, match='NUL'):
        space._command(b'backup', b'/tmp/a\0b')
    with pytest.raises(MalformedAtom):
        space.match('(a $x)', '($x')
    space._call.assert_not_called()
