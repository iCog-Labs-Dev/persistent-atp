"""
projector/writer.py — Project an event journal and save atoms as <proof_id>.metta
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Union

from .core import extract_proof_id, generate_metta_file, project_event_journal

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "proofs"


def safe_filename(name: str) -> str:
    """Make a proof_id safe to use as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "proof"


def project_journal_to_file(
    journal_path: Union[str, Path],
    output_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Project an event journal JSON file to a .metta file named after the proof.

    Args:
        journal_path: Path to the event journal JSON file.
        output_dir: Directory for the output file. Defaults to mork/proofs/.

    Returns:
        Path to the written <proof_id>.metta file.
    """
    journal_path = Path(journal_path)
    with journal_path.open("r", encoding="utf-8") as f:
        journal_data = json.load(f)

    proof_id = journal_data.get("proof_id")
    if not proof_id:
        events = journal_data.get("events", [])
        for event in events:
            for op in event.get("payload", {}).get("ops", []):
                node_id = op.get("id") or op.get("src") or op.get("dst") or ""
                if "/" in node_id:
                    proof_id = extract_proof_id(node_id)
                    break
            if proof_id:
                break
    if not proof_id:
        raise ValueError(f"No proof_id found in journal: {journal_path}")

    commands = project_event_journal(journal_data)

    out_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_filename(proof_id)}.metta"
    out_path.write_text(generate_metta_file(commands), encoding="utf-8")
    return out_path
