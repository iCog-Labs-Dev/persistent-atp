"""The first executable follow-up action shared by research and critic workers."""

from __future__ import annotations

from typing import Any, Mapping


STATEMENT_CHECK = "lean_statement_check"


def statement_check_action(raw: Any, declaration_ids: set[str]) -> dict[str, str]:
    """Accept only a known declaration and the supported action shape."""
    if not isinstance(raw, Mapping) or set(raw) != {"kind", "declaration_id"}:
        raise ValueError("statement check action needs kind and declaration_id")
    declaration_id = raw["declaration_id"]
    if raw["kind"] != STATEMENT_CHECK or not isinstance(declaration_id, str):
        raise ValueError("unsupported follow-up action")
    if declaration_id not in declaration_ids:
        raise ValueError("action cites an unknown draft declaration")
    return {"kind": STATEMENT_CHECK, "declaration_id": declaration_id}
