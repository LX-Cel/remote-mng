"""Build one host-native onedir artifact, then verify its frozen entry point."""
from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

from remote_mng.distribution import MANIFEST
from remote_mng.runtime import runtime_manifest


def build_wheel(root, output, version):
    """Publish a version-checked wheel for isolated uv tool installations."""
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(output), str(root)],
                   cwd=root, check=True)
    wheel = output / f"remote_mng-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise RuntimeError("Wheel build did not produce the expected universal package")
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise RuntimeError("Wheel must contain exactly one package metadata record")
        package = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
        if package.get("Name", "").replace("_", "-") != "remote-mng" or package.get("Version") != version:
            raise RuntimeError("Wheel metadata identity does not match the source runtime")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.with_suffix(".whl.sha256").write_text(f"{digest}  {wheel.name}\n", encoding="ascii")
    return {"path": str(wheel), "asset": wheel.name, "version": version,
            "sha256": digest, "bytes": wheel.stat().st_size}


def build(output):
    root = Path(__file__).resolve().parents[1]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = runtime_manifest()
    wheel = build_wheel(root, output, source["version"])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output / "onedir"), "--workpath", str(output / "work"),
                    str(root / "packaging/remote-mng.spec")], cwd=root, check=True)
    bundled = output / "onedir/rmg"
    executable = "rmg.exe" if source["platform"].startswith("windows-") else "rmg"
    raw = subprocess.run([str(bundled / executable), "--json", "runtime-info"], capture_output=True, check=True)
    verified = json.loads(raw.stdout.decode("utf-8"))
    if not verified.get("frozen") or verified["contracts"] != source["contracts"] or verified["version"] != source["version"]:
        raise RuntimeError("Frozen runtime identity does not match source")
    files = {}
    for item in sorted(bundled.rglob("*")):
        if item.is_file() and item.name != MANIFEST:
            # ZIP entries are materialized regular files; no unsafe archive links.
            files[item.relative_to(bundled).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    manifest = {"format": 1, "product": "remote-mng", "version": source["version"],
                "platform": source["platform"], "executable": executable, "contracts": source["contracts"],
                "files": files, "build_python": sys.version.split()[0],
                "runtime_requirements": {"glibc": platform.libc_ver()[1]} if sys.platform == "linux" else {"windows": "10-or-later"}}
    (bundled / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    artifact = output / f"remote-mng-{source['version']}-{source['platform']}.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in (*files, MANIFEST):
            archive.write(bundled / name, name)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum = artifact.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
    # One deterministic, platform-specific record lets the updater discover a
    # candidate then pin its exact ZIP and digest before any code is executed.
    release_metadata = {"format": 1, "product": "remote-mng", "version": source["version"],
                        "platform": source["platform"], "contracts": source["contracts"],
                        "asset": artifact.name, "sha256": digest, "bytes": artifact.stat().st_size,
                        "runtime_requirements": manifest["runtime_requirements"]}
    metadata_path = artifact.with_suffix(".release.json")
    metadata_path.write_text(json.dumps(release_metadata, indent=2) + "\n", encoding="utf-8")
    result = {"artifact": str(artifact), "sha256": digest, "bytes": artifact.stat().st_size,
              "platform": source["platform"], "version": source["version"], "frozen_probe": verified,
              "release_metadata": str(metadata_path), "wheel": wheel}
    (output / "build-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/release"))
    build(parser.parse_args().output)
