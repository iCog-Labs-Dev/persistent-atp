"""Install OmegaClaw plugins and the DeepSeek gateway route into an Omega copy.

Run against an isolated derived checkout, never the pinned source checkout.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "omegaclaw"
PLUGIN_FILES = ("persistent_atp.metta", "omega_persistent_atp_bridge.py",
                "deepseek_provider.py", "coordinator-policy.txt")
ROUTE_MARKER = "location = /deepseek/chat/completions {"


def install(omega_root: Path) -> dict:
    omega_root = omega_root.resolve()
    catalogue = omega_root / "config" / "plugins.yaml"
    proxy = omega_root / "proxy" / "nginx.conf.template"
    if not (omega_root / "src" / "plugin.py").is_file() or not catalogue.is_file() or not proxy.is_file():
        raise ValueError("--omega-root must be a full derived Omega checkout")
    plugin_dir = omega_root / "plugins" / "persistent_atp"
    source = PACKAGE_ROOT / "omega_plugin"
    for name in PLUGIN_FILES:
        target = plugin_dir / name
        if target.exists() and target.read_bytes() != (source / name).read_bytes():
            raise ValueError(f"derived Omega plugin file differs: {target}")

    original_catalogue = catalogue.read_text()
    overlay = (PACKAGE_ROOT / "config" / "omega-plugins.overlay.yaml").read_text()
    overlay = overlay.replace("/opt/persistent-atp/omega-plugin", str(plugin_dir))
    if all(f"- name: {name}" in original_catalogue for name in ("persistent_atp", "deepseek_provider")):
        if overlay.strip() not in original_catalogue:
            raise ValueError("existing Omega plugin catalogue entries differ from OmegaClaw overlay")
        updated_catalogue = original_catalogue
    else:
        for name in ("persistent_atp", "deepseek_provider"):
            if re.search(rf"(?m)^- name:\s*['\"]?{name}\b", original_catalogue):
                raise ValueError(f"Omega plugin name already exists: {name}")
        updated_catalogue = original_catalogue.rstrip() + "\n\n" + overlay.rstrip() + "\n"

    original_proxy = proxy.read_text()
    route = (PACKAGE_ROOT / "config" / "deepseek-nginx-location.conf").read_text().rstrip()
    if ROUTE_MARKER in original_proxy:
        if route not in original_proxy:
            raise ValueError("existing DeepSeek proxy route differs from OmegaClaw route")
        updated_proxy = original_proxy
    else:
        anchor = "        location /openrouter/ {"
        if original_proxy.count(anchor) != 1:
            raise ValueError("pinned Omega proxy template anchor is absent or ambiguous")
        updated_proxy = original_proxy.replace(anchor, route + "\n\n" + anchor, 1)

    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        shutil.copy2(source / name, plugin_dir / name)
    if updated_catalogue != original_catalogue:
        catalogue.write_text(updated_catalogue)
    if updated_proxy != original_proxy:
        proxy.write_text(updated_proxy)
    return {"omega_root": str(omega_root), "plugin_location": str(plugin_dir),
            "catalogue": str(catalogue), "proxy_template": str(proxy)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--omega-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.omega_root), sort_keys=True))
