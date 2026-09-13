"""Build one host-native onedir artifact, then verify its frozen entry point."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

from remote_mng.distribution import MANIFEST
from remote_mng.runtime import runtime_manifest


def build(output):
    root = Path(__file__).resolve().parents[1]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output / "onedir"), "--workpath", str(output / "work"),
                    str(root / "packaging/remote-mng.spec")], cwd=root, check=True)
    bundled = output / "onedir/rmg"
    source = runtime_manifest()
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
    result = {"artifact": str(artifact), "sha256": digest, "bytes": artifact.stat().st_size,
              "platform": source["platform"], "version": source["version"], "frozen_probe": verified}
    (output / "build-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/release"))
    build(parser.parse_args().output)
