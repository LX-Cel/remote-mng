# Build with the host OS/Python. No cross-compilation or one-file extraction.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files("remote_mng", includes=["assets/**/*"])
binaries = []
hiddenimports = ["win32timezone"] if sys.platform == "win32" else []
for package in ("asyncssh", "telnetlib3", "aiohttp", "mcp"):
    data, binary, hidden = collect_all(package, filter_submodules=lambda name: not name.startswith("mcp.cli"))
    datas += data
    binaries += binary
    hiddenimports += hidden
for package in ("remote-mng", "mcp", "cryptography", "asyncssh"):
    datas += copy_metadata(package, recursive=True)

a = Analysis([str(root / "packaging/frozen_entry.py")], pathex=[str(root / "src")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["pytest", "tkinter"],
             noarchive=False, optimize=1)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rmg", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True,
          disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rmg")
