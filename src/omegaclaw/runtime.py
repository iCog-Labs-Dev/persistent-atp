"""Reproducible Omega runtime selected by the bridge design."""
from pathlib import Path

OMEGA_REVISION = "ba185cd359d9fe231b904110b988af39fafa5014"
PETTA_REVISION = "7037f4c2ad378c52fc328004fe216d5118b674f0"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OMEGA_ROOT = ROOT / "tmp" / "repos" / "Omega-ba185cd"
