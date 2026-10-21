"""Embed one exact host-native release ZIP into an offline onefile installer.

The application remains onedir; only this short-lived standard-library launcher
uses PyInstaller onefile. Run after scripts/build_release.py, on each target OS.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "packaging/offline_setup.py"


def _launcher():
    specification = importlib.util.spec_from_file_location("offline_setup_build_validation", ENTRY)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def prepare(build_result, output, repository, prerelease):
    """Bind the installer to the original ZIP without changing a single byte."""
    launcher = _launcher()
    build = json.loads(build_result.read_text(encoding="utf-8"))
    version, target = build.get("version"), build.get("platform")
    if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or tuple(map(int, version.split("."))) < (0, 6, 0)
            or target != launcher.platform_tag() or target not in {"windows-x86_64", "linux-x86_64", "windows-arm64", "linux-arm64"}
            or not isinstance(build.get("sha256"), str) or not launcher._HASH.fullmatch(build["sha256"])
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)):
        raise ValueError("Build metadata must identify an exact native release >= 0.6.0 with the offline bootstrap protocol")
    archive = Path(build["artifact"]).expanduser().resolve()
    if archive.name != f"remote-mng-{version}-{target}.zip" or launcher.digest(archive) != build["sha256"].lower():
        raise ValueError("The original release ZIP differs from its pinned build metadata")
    with zipfile.ZipFile(archive) as source:
        if source.getinfo(launcher.MANIFEST).file_size > launcher.MAX_METADATA:
            raise ValueError("The release manifest exceeds its size limit")
        manifest_raw = source.read(launcher.MANIFEST)
        manifest = launcher._json(manifest_raw.decode("utf-8"))
    selected = {"format": 1, "product": "remote-mng", "repository": repository,
                "tag": "v" + version, "version": version, "platform": target,
                "archive": archive.name, "sha256": build["sha256"].lower(), "prerelease": bool(prerelease),
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(), "contracts": manifest.get("contracts")}
    payload = output / "offline-input"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / launcher.METADATA).write_text(json.dumps(selected, indent=2) + "\n", encoding="utf-8")
    # Validate before PyInstaller embeds anything. A temporary stage is used only
    # to read/hash files; no candidate code is executed during this packaging step.
    launcher.metadata(payload)
    with tempfile.TemporaryDirectory(prefix="rmg-offline-build-") as temporary:
        launcher.extract(archive, selected, Path(temporary))
    shutil.copyfile(archive, payload / launcher.PAYLOAD)
    if launcher.digest(payload / launcher.PAYLOAD) != selected["sha256"]:
        raise ValueError("The embedded ZIP copy differs from the original release")
    return build, selected, payload


def build_installer(build_result, output=None, *, repository="LX-Cel/remote-mng", prerelease=False):
    build_result = Path(build_result).resolve()
    output = Path(output).resolve() if output is not None else build_result.parent / "offline"
    # PyInstaller's output/spec/work files belong to an output directory, never
    # beside the committed entry point or under the package's source tree.
    if output == ROOT or output.is_relative_to(ROOT / "packaging") or output.is_relative_to(ROOT / "src"):
        raise ValueError("Choose a separate build output directory")
    output.mkdir(parents=True, exist_ok=True)
    package, selected, payload = prepare(build_result, output, repository, prerelease)
    name = f"remote-mng-{selected['version']}-{selected['platform']}-setup"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
        "--noupx", "--name", name, "--distpath", str(output / "bin"),
        "--workpath", str(output / "work"), "--specpath", str(output / "spec"),
        "--add-data", os.pathsep.join((str(payload / "offline-metadata.json"), ".")),
        "--add-data", os.pathsep.join((str(payload / "payload.zip"), ".")), str(ENTRY)],
        cwd=ROOT, check=True, stdout=sys.stderr)
    built = output / "bin" / (name + (".exe" if selected["platform"].startswith("windows-") else ""))
    installer = output / (name + (".exe" if selected["platform"].startswith("windows-") else ".run"))
    shutil.copyfile(built, installer)
    installer.chmod(0o755)
    launcher = _launcher()
    checksum = launcher.digest(installer)
    installer.with_name(installer.name + ".sha256").write_text(f"{checksum}  {installer.name}\n", encoding="ascii")
    result = {"format": 1, "product": "remote-mng", "version": selected["version"], "platform": selected["platform"],
              "artifact": str(installer), "installer": str(installer), "asset": installer.name,
              "sha256": checksum, "bytes": installer.stat().st_size,
              "payload_asset": selected["archive"], "payload_sha256": selected["sha256"],
              "payload": {"artifact": str(Path(package["artifact"]).resolve()), "sha256": selected["sha256"],
                          "bytes": (payload / "payload.zip").stat().st_size},
              "metadata": selected, "runtime": "installer-only-onefile", "network_required": False}
    result["inputs"] = {"build_result": str(build_result), "entry": str(ENTRY), "entry_sha256": launcher.digest(ENTRY)}
    (output / "offline-installer-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-result", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repository", default="LX-Cel/remote-mng")
    parser.add_argument("--prerelease", action="store_true")
    args = parser.parse_args()
    build_installer(args.build_result, args.output, repository=args.repository, prerelease=args.prerelease)
