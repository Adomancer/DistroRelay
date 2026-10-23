"""Peta nama paket logis -> nama paket per distro (`data/packages.toml`).

Kunci per entri: ID distro (ubuntu, linuxmint, ...), keluarga distro (fedora, arch, debian), atau `all`, dengan
urutan prioritas itu. Daftar kosong = tidak tersedia di distro itu (dilewati, dengan catatan). Nama yang tidak ada
di peta dipakai apa adanya di semua distro.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .profile import FAMILIES

_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._+:-]*$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PackageMapError(ValueError):
    pass


@dataclass
class Resolution:
    packages: list[str] = field(default_factory=list)  # nama paket asli, urut, tanpa dobel
    skipped: list[str] = field(default_factory=list)  # nama logis yang tidak tersedia di distro ini
    mapping: dict[str, list[str]] = field(default_factory=dict)  # nama logis -> paket asli (untuk laporan)


@dataclass
class PackageMap:
    entries: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def lookup(self, name: str, distro_id: str, family: str) -> list[str] | None:
        """Paket asli untuk satu nama logis. None = tidak ada di peta (pakai nama yang sama)."""
        entry = self.entries.get(name)
        if entry is None:
            return None
        for key in (distro_id, family, "all"):
            if key in entry:
                return list(entry[key])
        return None

    def resolve(self, names: list[str], distro_id: str, family: str) -> Resolution:
        result = Resolution()
        for name in names:
            packages = self.lookup(name, distro_id, family)
            if packages is None:
                packages = [name]
            if not packages:
                result.skipped.append(name)
                continue
            result.mapping[name] = packages
            for package in packages:
                if package not in result.packages:
                    result.packages.append(package)
        return result

    def reverse(self, installed: list[str], distro_id: str, family: str) -> tuple[list[str], list[str]]:
        """Paket asli yang terpasang -> (nama logis yang semua paketnya terpasang, paket yang tidak ter-map)."""
        have = set(installed)
        logical, covered = [], set()
        for name in sorted(self.entries):
            packages = self.lookup(name, distro_id, family)
            if packages and all(p in have for p in packages):
                logical.append(name)
                covered.update(packages)
        unmapped = [p for p in installed if p not in covered]
        return logical, unmapped

    def merged(self, override: "PackageMap") -> "PackageMap":
        """Peta ini ditimpa per nama logis oleh `override` (mis. packages.toml milik pengguna)."""
        entries = {k: dict(v) for k, v in self.entries.items()}
        for name, value in override.entries.items():
            entries.setdefault(name, {}).update(value)
        return PackageMap(entries)

    def to_json(self) -> str:
        return json.dumps({"version": 1, "packages": self.entries}, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def from_dict(data: dict) -> PackageMap:
    if data is not None and not isinstance(data, dict):
        raise PackageMapError("peta paket harus tabel: nama logis -> distro -> daftar paket")
    problems = []
    entries: dict[str, dict[str, list[str]]] = {}
    for name, value in (data or {}).items():
        if not _NAME.match(name):
            problems.append(f"{name!r}: nama logis tidak valid")
            continue
        if not isinstance(value, dict):
            problems.append(f"{name}: harus tabel distro -> daftar paket")
            continue
        entries[name] = {}
        for key, packages in value.items():
            if not _KEY.match(key):
                problems.append(f"{name}.{key}: kunci distro tidak valid")
            elif not isinstance(packages, list) or not all(isinstance(p, str) and _NAME.match(p) for p in packages):
                problems.append(f"{name}.{key}: harus daftar nama paket yang valid")
            else:
                entries[name][key] = list(packages)
    if problems:
        raise PackageMapError("; ".join(problems))
    return PackageMap(entries)


def from_json(text: str) -> PackageMap:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PackageMapError(f"packages.json bukan JSON yang valid: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PackageMapError("packages.json: versi tidak didukung")
    return from_dict(data.get("packages") or {})


def from_toml(text: str) -> PackageMap:
    """Hanya di sisi Windows (butuh tomllib, Python 3.11+)."""
    import tomllib

    try:
        return from_dict(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise PackageMapError(f"packages.toml tidak valid: {exc}") from exc


def builtin() -> PackageMap:
    """Peta bawaan DistroRelay (data/packages.toml)."""
    from pathlib import Path

    return from_toml((Path(__file__).parent / "data" / "packages.toml").read_text(encoding="utf-8"))


__all__ = ["FAMILIES", "PackageMap", "PackageMapError", "Resolution", "builtin", "from_dict", "from_json", "from_toml"]
