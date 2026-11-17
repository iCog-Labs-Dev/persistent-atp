"""A sealed image verifies runtime bytes without retaining a Git checkout."""
import json

import pytest

from omegaclaw.runtime import MANIFEST, OMEGA_REVISION, seal_runtime, verify_checkout, verify_manifest
from omegaclaw.installer import bundle_runtime


@pytest.fixture
def sealed(tmp_path):
    for name in ("src/loop.metta", "src/plugin.py", "run.metta", "persistent_atp_pinned_import.pl"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trusted-build-fixture\n")
    seal_runtime(tmp_path)
    return tmp_path


def test_sealed_runtime_verifies_without_git_metadata(sealed):
    verify_checkout(sealed)
    assert json.loads((sealed / MANIFEST).read_text())["revision"] == OMEGA_REVISION


def test_container_overlay_is_sealed_and_requires_a_digest(sealed, tmp_path):
    # A sibling of the runtime, never nested in it.
    output = sealed.parent / (sealed.name + "-image")
    with pytest.raises(ValueError, match="pinned"):
        bundle_runtime(sealed, output, "omega:latest")
    result = bundle_runtime(sealed, output, "omega@sha256:" + "a" * 64)
    verify_manifest(output / "omega")
    dockerfile = (output / "Dockerfile").read_text()
    assert "FROM omega@sha256:" in dockerfile and "@@" not in dockerfile
    assert not (output / "omega/.git").exists()
    with pytest.raises(FileExistsError):
        bundle_runtime(sealed, output, result["base_image"])


@pytest.mark.parametrize("tamper", ["core", "revision", "escape", "empty"])
def test_sealed_runtime_rejects_altered_or_incomplete_manifest(sealed, tamper):
    path = sealed / MANIFEST
    data = json.loads(path.read_text())
    if tamper == "core":
        (sealed / "src/loop.metta").write_text("changed\n")
    elif tamper == "revision":
        data["revision"] = "0" * 40
    elif tamper == "escape":
        data["files"]["../outside"] = "0" * 64
    else:
        data["files"] = {}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError): verify_manifest(sealed)
