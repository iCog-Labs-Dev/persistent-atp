"""Run the packaged DeepSeek probe from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omegaclaw.deepseek_probe import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
