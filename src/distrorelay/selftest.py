"""`distrorelay selftest`: cek isi paket (terutama .exe hasil PyInstaller) tanpa mengubah sistem.

Yang dicek: data bawaan (peta paket, daftar paket bawaan distro, keyring ISO), kode Linux kit bisa diekspor ke folder
sementara dan lolos verifikasi hash, dan TUI bisa tampil (headless, dengan hasil scan kosong). gpgv hanya dilaporkan.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class SelfCheck:
    name: str
    ok: bool
    message: str


def _check(name: str, fn: Callable[[], str]) -> SelfCheck:
    try:
        return SelfCheck(name, True, fn())
    except Exception as exc:  # setiap cek berdiri sendiri; laporkan semuanya
        return SelfCheck(name, False, f"{type(exc).__name__}: {exc}")


def _version() -> str:
    import distrorelay

    mode = "exe (PyInstaller)" if getattr(sys, "frozen", False) else f"Python {sys.version.split()[0]}"
    return f"DistroRelay {distrorelay.__version__}, {mode}"


def _package_data() -> str:
    from distrorelay_linux import backup as linux_backup

    from .common import packages

    entries = len(packages.builtin().entries)
    families = sorted(p.stem for p in linux_backup._BASE_DIR.glob("*.txt"))
    if families != ["arch", "debian", "fedora"]:
        raise FileNotFoundError(f"base_packages tidak lengkap: {families}")
    return f"{entries} nama paket logis; base_packages {', '.join(families)}"


def _kit_export() -> str:
    from distrorelay_linux import kit as linux_kit

    from .common import profile as profile_mod
    from .common import packages
    from .windows import kit_export

    sources = kit_export.kit_sources()
    missing = [rel for rel, path in sources if not path.is_file()]
    if missing or not any(rel == "bootstrap.sh" for rel, _ in sources):
        raise FileNotFoundError(f"file kit tidak ada: {missing or ['bootstrap.sh']}")
    with tempfile.TemporaryDirectory(prefix="distrorelay-selftest-") as temp:
        result = kit_export.export_kit(profile_mod.template(), packages.builtin(), Path(temp))
        problems = result.problems or linux_kit.verify(result.kit_dir)
        if problems:
            raise RuntimeError("; ".join(problems[:3]))
        return f"{len(result.files)} file diekspor ke folder sementara dan lolos verifikasi"


def _iso_keys() -> str:
    from .windows.iso import sources

    missing = [name for name, source in sources.SOURCES.items() if not source.KEYS.path.is_file()]
    if missing:
        raise FileNotFoundError(f"keyring tidak ada: {', '.join(missing)}")
    return f"keyring {', '.join(sources.SOURCES)} ada"


def _gpgv() -> str:
    from .windows.iso import verify

    found = verify.find_gpgv()
    return found or f"tidak ditemukan (dibutuhkan `iso download/verify/copy`; pasang: {verify.INSTALL_HINT})"


async def _tui_headless() -> str:
    import distrorelay

    from .tui.app import DistroRelayApp
    from .windows.scanner import ScanReport

    report = ScanReport("selftest", distrorelay.__version__, False, "selftest")
    app = DistroRelayApp(scan=lambda progress=None: report)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        screen = type(app.screen).__name__
    return f"{screen} tampil (headless)"


def run() -> list[SelfCheck]:
    return [
        _check("Versi", _version),
        _check("Data bawaan", _package_data),
        _check("Linux kit", _kit_export),
        _check("Keyring ISO", _iso_keys),
        _check("gpgv", _gpgv),
        _check("TUI", lambda: asyncio.run(_tui_headless())),
    ]
