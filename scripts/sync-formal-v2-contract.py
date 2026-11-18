"""Vendor the provider's generated schemas and worked fixtures without redefining them."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def address(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, type=Path)
    args = parser.parse_args()
    source = args.provider.resolve() / "contracts/formal-v2"
    raw = (source / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    for name, expected in manifest["files_sha256"].items():
        if address((source / name).read_bytes()) != expected:
            raise ValueError(f"provider contract file is not verified: {name}")
    root = Path(__file__).resolve().parents[1]
    destination = root / "src/mathproof/contracts/formal-v2"
    (destination / "schemas").mkdir(parents=True, exist_ok=True)
    for name in manifest["schema_canonical_sha256"]:
        shutil.copyfile(source / name, destination / name)
    (destination / "manifest.json").write_bytes(raw)
    fixtures = root / "fixtures/formal_v2/examples"
    fixtures.mkdir(parents=True, exist_ok=True)
    for path in (source / "examples").glob("*.json"):
        shutil.copyfile(path, fixtures / path.name)
    print("Verified provider contract copied. Pin MANIFEST_HASH to:", address(raw))


if __name__ == "__main__":
    main()
