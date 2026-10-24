"""Run the packaged OmegaClaw installer from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omegaclaw.installer import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
