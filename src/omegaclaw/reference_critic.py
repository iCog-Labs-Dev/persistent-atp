"""Explicitly scripted critic process for the reference profile only."""
import sys


def main() -> int:
    # The trusted service compiled and validated this input packet. The
    # reference worker intentionally makes no mathematical judgment.
    sys.stdin.buffer.read(65_536)
    sys.stdout.buffer.write(b'{"status":"pending","mode":"scripted-reference-critic"}')
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
