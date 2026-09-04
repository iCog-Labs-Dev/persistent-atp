"""Un-shadow the Neo4j driver for this test package.

The repo's own top-level ``neo4j/`` package has the same name as the driver
distribution, so with the repository root on ``sys.path`` (which is what
``python -m pytest`` does) ``import neo4j`` finds the projection instead of the
driver. Dropping the repository root lets these tests import
``neo4j.exceptions`` while still loading the projection explicitly by path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

for entry in ("", ".", os.getcwd(), str(REPO_ROOT)):
    while entry in sys.path:
        sys.path.remove(entry)

# A partially initialised projection may already be cached under the name.
if (getattr(sys.modules.get("neo4j"), "__file__", None) or "").startswith(str(REPO_ROOT)):
    del sys.modules["neo4j"]
