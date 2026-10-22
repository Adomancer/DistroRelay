"""Kenali distro lewat /etc/os-release (ID + ID_LIKE), jadi turunannya ikut terdeteksi.

EndeavourOS/Manjaro -> arch, Nobara -> fedora, Mint/Pop!_OS -> debian (lewat ubuntu).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

OS_RELEASE_PATHS = ("/etc/os-release", "/usr/lib/os-release")

# Token ID/ID_LIKE -> keluarga yang didukung (package manager + penamaan paket).
_FAMILY_OF = {"fedora": "fedora", "arch": "arch", "debian": "debian", "ubuntu": "debian"}


@dataclass(frozen=True)
class Distro:
    id: str
    id_like: tuple[str, ...]
    version_id: str
    pretty_name: str
    family: str | None  # "fedora" | "arch" | "debian" | None (tidak didukung)

    @property
    def describe(self) -> str:
        family = self.family or "tidak didukung"
        return f"{self.pretty_name or self.id} (ID {self.id}, keluarga {family})"


def parse_os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parsed = shlex.split(raw)
        except ValueError:
            continue
        values[key.strip()] = parsed[0] if parsed else ""
    return values


def detect(text: str) -> Distro:
    values = parse_os_release(text)
    distro_id = values.get("ID", "").lower()
    id_like = tuple(values.get("ID_LIKE", "").lower().split())
    family = next((_FAMILY_OF[token] for token in (distro_id,) + id_like if token in _FAMILY_OF), None)
    return Distro(distro_id, id_like, values.get("VERSION_ID", ""), values.get("PRETTY_NAME", ""), family)


def read() -> Distro:
    for path in OS_RELEASE_PATHS:
        try:
            return detect(Path(path).read_text(encoding="utf-8"))
        except OSError:
            continue
    return Distro("", (), "", "", None)
