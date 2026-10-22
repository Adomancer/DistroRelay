"""Export Linux kit (M4): kode kit + profil ke USB dan/atau partisi NTFS Windows.

Kit ditulis ke folder sementara di drive tujuan, diverifikasi (hash dibaca ulang), baru menggantikan kit lama.
Kit sebelumnya disimpan sebagai `kit.prev`. Folder `linux-backups` (backup dari Linux, M5) tidak pernah disentuh.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import distrorelay
import distrorelay_linux
from distrorelay_linux import KIT_FORMAT
from distrorelay_linux import kit as linux_kit

from ..common import packages as packages_mod
from ..common import profile as profile_mod
from ..paths import app_data_dir

_TEXT_SUFFIXES = (".py", ".sh", ".json", ".toml", ".txt")
# Fast Startup = sesi kernel Windows di-hibernate saat shutdown; NTFS-nya dianggap "masih dipakai", jadi Linux
# hanya bisa me-mount-nya read-only (atau menolak). Kit bisa dibaca, tapi backup dari Linux (M5) tidak bisa ditulis.
POWER_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Power"


class KitExportError(RuntimeError):
    pass


def profile_path() -> Path:
    return app_data_dir() / "profile.toml"


def user_packages_path() -> Path:
    return app_data_dir() / "packages.toml"


def load_profile(path: Path | None = None) -> profile_mod.Profile:
    path = path or profile_path()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise profile_mod.ProfileError([f"{path} belum ada; buat dulu dengan `distrorelay profile init`"]) from None
    return profile_mod.from_toml(text)


def load_package_map(user_path: Path | None = None) -> packages_mod.PackageMap:
    """Peta bawaan, ditimpa per nama oleh packages.toml milik pengguna kalau ada."""
    base = packages_mod.builtin()
    user_path = user_path or user_packages_path()
    if Path(user_path).is_file():
        return base.merged(packages_mod.from_toml(Path(user_path).read_text(encoding="utf-8")))
    return base


def kit_sources() -> list[tuple[str, Path]]:
    """(path di kit, file sumber): kode bersama + kode Linux kit + bootstrap.sh."""
    core = Path(distrorelay.__file__).parent
    linux = Path(distrorelay_linux.__file__).parent
    files = [("distrorelay/__init__.py", core / "__init__.py")]
    files += [(f"distrorelay/common/{p.name}", p) for p in sorted((core / "common").glob("*.py"))]
    base = core / "common" / "data" / "base_packages"  # filter paket sistem saat backup (M5)
    files += [(f"distrorelay/common/data/base_packages/{p.name}", p) for p in sorted(base.glob("*.txt"))]
    files += [(f"distrorelay_linux/{p.name}", p) for p in sorted(linux.glob("*.py"))]
    files.append(("bootstrap.sh", linux / "bootstrap.sh"))
    return files


def _normalize(rel: str, data: bytes) -> bytes:
    """Teks selalu LF: bootstrap.sh dengan CRLF gagal dijalankan di Linux ('/bin/sh^M')."""
    return data.replace(b"\r\n", b"\n") if rel.endswith(_TEXT_SUFFIXES) else data


@dataclass
class ExportResult:
    kit_dir: Path
    created_at: str
    files: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    previous: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def _is_kit(path: Path) -> bool:
    return (path / linux_kit.MANIFEST).is_file()


def export_kit(
    profile: profile_mod.Profile,
    package_map: packages_mod.PackageMap,
    root: Path,
    *,
    now: datetime | None = None,
) -> ExportResult:
    now = now or datetime.now()
    base = Path(root) / "DistroRelay"
    kit_dir, previous = base / "kit", base / "kit.prev"
    staging = base / f"kit.new-{now:%Y%m%d-%H%M%S}"
    if kit_dir.exists() and not _is_kit(kit_dir):
        raise KitExportError(f"{kit_dir} sudah ada tapi bukan kit DistroRelay; pindahkan dulu isinya")
    created_at = now.astimezone().isoformat(timespec="seconds")

    contents = [(rel, _normalize(rel, source.read_bytes())) for rel, source in kit_sources()]
    contents.append(("profile.json", profile_mod.to_json(profile).encode("utf-8")))
    contents.append(("packages.json", package_map.to_json().encode("utf-8")))

    staging.mkdir(parents=True, exist_ok=False)
    try:
        files = []
        for rel, data in contents:
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {
            "format": KIT_FORMAT,
            "distrorelay": distrorelay.__version__,
            "created_at": created_at,
            "profile": profile.name,
            "files": files,
        }
        (staging / linux_kit.MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        problems = linux_kit.verify(staging)
        if problems:
            raise KitExportError("Salinan kit tidak cocok setelah ditulis: " + "; ".join(problems[:3]))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if previous.exists():
        if not _is_kit(previous):
            raise KitExportError(f"{previous} ada tapi bukan kit DistroRelay; pindahkan dulu")
        shutil.rmtree(previous)
    moved = None
    if kit_dir.exists():
        kit_dir.rename(previous)
        moved = previous
    try:
        staging.rename(kit_dir)
    except OSError:
        if moved is not None:
            previous.rename(kit_dir)  # kit lama kembali; kit baru tetap ada di folder staging
        raise
    return ExportResult(kit_dir, created_at, files, linux_kit.verify(kit_dir), moved)


# --- Fast Startup ------------------------------------------------------------------


def fast_startup_enabled() -> bool | None:
    """True/False dari HiberbootEnabled; None kalau tidak terbaca (bukan Windows, atau kunci tidak ada)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POWER_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "HiberbootEnabled")
        return bool(value)
    except (ImportError, OSError):
        return None


def set_fast_startup(enabled: bool) -> None:
    """Butuh Administrator. Berlaku mulai shutdown berikutnya."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POWER_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "HiberbootEnabled", 0, winreg.REG_DWORD, 1 if enabled else 0)
