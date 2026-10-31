"""Disposable-file statement elaboration; this never certifies a proof."""

from __future__ import annotations

import subprocess
import tempfile
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class StatementCheck:
    ok: bool
    diagnostics: str


class LeanStatementChecker:
    """Elaborate a draft proposition in a disposable file.

    The temporary `sorry` is only an elaboration placeholder, never a
    committed declaration or proof. Run this checker in an OS sandbox when
    model-generated Lean is untrusted.
    """

    def __init__(self, project_dir: str | Path, *, timeout_seconds: float = 30.0):
        self.project_dir = Path(project_dir).resolve()
        self.timeout_seconds = timeout_seconds

    def check(self, lean_type: str, imports: Sequence[str]) -> StatementCheck:
        import_name = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")
        forbidden = re.compile(r"\b(?:sorry|admit|axiom|unsafe|run_tac|set_option|by)\b|#|;|\n", re.I)
        if (
            not isinstance(lean_type, str) or not lean_type.strip() or forbidden.search(lean_type)
            or not imports or any(not isinstance(item, str) or not import_name.fullmatch(item) for item in imports)
        ):
            return StatementCheck(False, "unsafe or malformed statement input")
        source = "\n".join(f"import {module}" for module in imports)
        source += f"\n\nset_option autoImplicit false\nexample : {lean_type} := by\n  sorry\n"
        with tempfile.TemporaryDirectory(prefix="lean-statement-") as directory:
            path = Path(directory) / "Statement.lean"
            path.write_text(source, encoding="utf-8")
            try:
                completed = subprocess.run(
                    ["lake", "env", "lean", str(path)],
                    cwd=self.project_dir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return StatementCheck(False, str(exc)[:4000])
        diagnostics = (completed.stdout + completed.stderr)[:4000]
        return StatementCheck(completed.returncode == 0, diagnostics)
