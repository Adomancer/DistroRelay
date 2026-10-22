# PyInstaller spec untuk distrorelay.exe (M7). Build dari folder repo:
#   .venv\Scripts\python.exe -m PyInstaller packaging\distrorelay.spec --noconfirm
# Hasil: dist\distrorelay.exe (satu file, aplikasi konsol). Cek isinya dengan `dist\distrorelay.exe selftest`.
#
# Tidak memakai --uac-admin: manifest admin membuat setiap perintah (juga `iso`, `profile`, `kit` yang tidak butuh
# admin) membuka jendela UAC baru dan output-nya hilang saat jendela itu tertutup. Elevasi tetap lewat DistroRelay
# sendiri (relaunch "runas" hanya untuk perintah yang butuh admin), sama seperti saat dijalankan dengan Python.

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
VERSION = re.search(r'__version__ = "([^"]+)"', (SRC / "distrorelay" / "__init__.py").read_text()).group(1)
NUMBERS = tuple(int(n) for n in (VERSION.split(".") + ["0", "0", "0"])[:4])

# Linux kit diekspor dari file sumber .py (bukan bytecode di dalam .exe), jadi file-file itu ikut sebagai data.
datas = [
    (str(SRC / "distrorelay" / "__init__.py"), "distrorelay"),
    (str(SRC / "distrorelay" / "common" / "*.py"), "distrorelay/common"),
    (str(SRC / "distrorelay" / "common" / "data" / "*.toml"), "distrorelay/common/data"),
    (str(SRC / "distrorelay" / "common" / "data" / "base_packages" / "*.txt"), "distrorelay/common/data/base_packages"),
    (str(SRC / "distrorelay" / "windows" / "iso" / "keys" / "*.gpg"), "distrorelay/windows/iso/keys"),
    (str(SRC / "distrorelay_linux" / "*.py"), "distrorelay_linux"),
    (str(SRC / "distrorelay_linux" / "bootstrap.sh"), "distrorelay_linux"),
]
datas += collect_data_files("textual")

# Textual memuat widget secara lazy, Rich memuat tabel unicode lewat import_module, dan CLI meng-import modulnya
# di dalam fungsi; semuanya diikutkan supaya tidak ada ImportError saat .exe dijalankan.
hiddenimports = (
    collect_submodules("textual")
    + collect_submodules("rich")
    + collect_submodules("distrorelay")
    + collect_submodules("distrorelay_linux")
)

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "tkinter", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "DistroRelay"),
                StringStruct("FileDescription", "DistroRelay: toolkit distro-hopping dual-boot Windows + Linux"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", "distrorelay"),
                StringStruct("OriginalFilename", "distrorelay.exe"),
                StringStruct("ProductName", "DistroRelay"),
                StringStruct("ProductVersion", VERSION),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="distrorelay",
    console=True,
    upx=False,
    version=version_info,
)
