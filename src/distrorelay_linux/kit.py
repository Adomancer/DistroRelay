"""Folder kit: profil + kode, dengan manifest SHA256 yang dicek ulang sebelum dipakai.

Tata letak (dibuat oleh `distrorelay kit export` di Windows):
    <drive>/DistroRelay/kit/            kode + profile.json + packages.json + manifest.json (diganti tiap export)
    <drive>/DistroRelay/linux-backups/  backup dari distro lama (M5), tidak pernah disentuh export
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from distrorelay.common import packages as packages_mod
from distrorelay.common import profile as profile_mod

from . import KIT_FORMAT

MANIFEST = "manifest.json"


class KitError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Kit:
    root: Path
    manifest: dict
    profile: profile_mod.Profile
    packages: packages_mod.PackageMap

    @property
    def created_at(self) -> str:
        return str(self.manifest.get("created_at", ""))

    @property
    def backups_dir(self) -> Path:
        return self.root.parent / "linux-backups"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(root: Path) -> dict:
    try:
        manifest = json.loads((Path(root) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KitError([f"{MANIFEST} tidak bisa dibaca: {exc}"]) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != KIT_FORMAT:
        raise KitError([f"Format kit {manifest.get('format') if isinstance(manifest, dict) else '?'} tidak didukung"])
    return manifest


# File yang bisa dijalankan/dibaca kit: yang tidak tercatat di manifest ditolak (selain __pycache__).
_CODE_SUFFIXES = (".py", ".sh", ".json", ".txt", ".toml")
# File inti yang wajib tercatat di manifest; kit tanpa salah satunya tidak dipakai.
REQUIRED_FILES = ("profile.json", "packages.json", "bootstrap.sh", "distrorelay_linux/__init__.py",
                  "distrorelay_linux/cli.py")


def verify(root: Path) -> list[str]:
    """Masalah integritas kit (kosong = semua file sama dengan manifest)."""
    root = Path(root)
    try:
        manifest = read_manifest(root)
    except KitError as exc:
        return exc.problems
    problems = []
    listed = {str(entry.get("path", "")) for entry in manifest.get("files", [])}
    for required in REQUIRED_FILES:
        if required not in listed:
            problems.append(f"{required}: tidak tercatat di manifest")
    root_resolved = root.resolve()
    for path in sorted(root_resolved.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix not in _CODE_SUFFIXES:
            continue
        rel = path.relative_to(root_resolved).as_posix()
        if rel != MANIFEST and rel not in listed:
            problems.append(f"{rel}: ada di folder kit tapi tidak tercatat di manifest")
    for entry in manifest.get("files", []):
        rel = str(entry.get("path", ""))
        target = (root / rel).resolve()
        if not rel or rel.startswith("/") or ".." in Path(rel).parts or root.resolve() not in target.parents:
            problems.append(f"{rel!r}: path tidak valid di manifest")
        elif not target.is_file():
            problems.append(f"{rel}: hilang")
        elif sha256_file(target) != entry.get("sha256"):
            problems.append(f"{rel}: isinya berubah (hash tidak cocok)")
    return problems


def load(root: Path) -> Kit:
    root = Path(root)
    problems = verify(root)
    if problems:
        raise KitError(problems)
    manifest = read_manifest(root)
    try:
        profile = profile_mod.from_json((root / "profile.json").read_text(encoding="utf-8"))
        packages = packages_mod.from_json((root / "packages.json").read_text(encoding="utf-8"))
    except (OSError, profile_mod.ProfileError, packages_mod.PackageMapError) as exc:
        raise KitError([str(exc)]) from exc
    return Kit(root, manifest, profile, packages)


def default_search_roots(user: str | None = None) -> list[Path]:
    """Tempat drive biasa di-mount: media desktop, /mnt (termasuk /mnt/c di WSL)."""
    user = user or os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    roots = [Path("/run/media") / user, Path("/media") / user, Path("/media"), Path("/mnt")]
    return [r for r in roots if r.is_dir()]


def find_kits(search_roots: list[Path]) -> list[tuple[str, Path]]:
    """Kit yang ditemukan (created_at, folder), terbaru dulu. Folder rusak dilewati."""
    found = {}
    for base in search_roots:
        try:
            mounts = [p for p in Path(base).iterdir() if p.is_dir()]
        except OSError:
            continue
        for mount in mounts:
            candidate = mount / "DistroRelay" / "kit"
            if (candidate / MANIFEST).is_file():
                try:
                    found[candidate.resolve()] = str(read_manifest(candidate).get("created_at", ""))
                except KitError:
                    continue
    return sorted(((created, path) for path, created in found.items()), reverse=True)


def here() -> Path:
    """Folder kit tempat kode ini dijalankan (kit/distrorelay_linux/.. = kit/)."""
    return Path(__file__).resolve().parent.parent
