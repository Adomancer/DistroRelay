"""ISO manager (M6): cek versi terbaru, unduh, verifikasi GPG + SHA256, salin ke USB Ventoy.

Kepercayaan hanya berasal dari keyring yang disertakan di aplikasi (`keys/`) dan fingerprint yang dicatat di
kode sumber tiap distro. Feed versi, listing mirror, dan nilai SHA256 dari feed hanya dipakai untuk menemukan
file; ISO baru dianggap sah kalau tanda tangannya cocok dengan kunci yang di-pin dan SHA256-nya cocok.

Unduhan disimpan di %LOCALAPPDATA%\\DistroRelay\\iso\\<distro>\\<versi>-<edisi>\\ bersama `release.json`
(dari mana file diambil) dan `verified.json` (hasil verifikasi terakhir).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ...paths import app_data_dir

KEYS_DIR = Path(__file__).parent / "keys"
RELEASE_FILE = "release.json"
VERIFIED_FILE = "verified.json"

# Cara distro menandatangani rilisnya (lihat tabel di plan).
CLEARSIGNED_CHECKSUM = "clearsigned-checksum"  # Fedora: file CHECKSUM ditandatangani inline
DETACHED_CHECKSUM = "detached-checksum"  # Ubuntu, Mint: SHA256SUMS + SHA256SUMS.gpg
DETACHED_ISO = "detached-iso"  # Arch: ISO-nya langsung (.iso.sig)
SCHEMES = (CLEARSIGNED_CHECKSUM, DETACHED_CHECKSUM, DETACHED_ISO)


class IsoError(RuntimeError):
    """Feed tidak terbaca, file tidak ada, atau verifikasi gagal."""


def iso_dir() -> Path:
    return app_data_dir() / "iso"


def version_key(version: str) -> tuple[int, ...]:
    """'22.3' -> (22, 3); '2026.10.01' -> (2026, 10, 1). Untuk membandingkan versi."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


@dataclass(frozen=True)
class KeySpec:
    """Keyring bawaan + fingerprint kunci utama yang boleh menandatangani rilis distro ini."""

    path: Path
    fingerprints: frozenset[str]

    @classmethod
    def bundled(cls, name: str, fingerprints: set[str]) -> "KeySpec":
        return cls(KEYS_DIR / name, frozenset(f.upper() for f in fingerprints))


@dataclass(frozen=True)
class Release:
    distro: str
    version: str
    edition: str
    iso_name: str
    iso_url: str
    scheme: str
    checksum_url: str | None = None  # file checksum yang ditandatangani (bukan Arch)
    signature_url: str | None = None  # tanda tangan terpisah (bukan Fedora)
    size: int | None = None  # dari feed, untuk cek ukuran unduhan
    sha256: str | None = None  # dari feed (tidak ditandatangani); untuk Arch jadi pembanding lapis kedua

    @property
    def label(self) -> str:
        return f"{self.distro} {self.version} ({self.edition})"

    @property
    def folder_name(self) -> str:
        return re.sub(r"[^\w.-]", "_", f"{self.version}-{self.edition}")

    @property
    def checksum_name(self) -> str | None:
        return _basename(self.checksum_url)

    @property
    def signature_name(self) -> str | None:
        return _basename(self.signature_url)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Release":
        release = cls(**{k: data.get(k) for k in cls.__dataclass_fields__})
        problems = release.problems()
        if problems:
            raise IsoError("; ".join(problems))
        return release

    def problems(self) -> list[str]:
        problems = []
        for name in (self.iso_name, self.checksum_name, self.signature_name):
            if name is not None and not _SAFE_NAME.match(name):
                problems.append(f"nama file tidak aman: {name!r}")
        if self.scheme not in SCHEMES:
            problems.append(f"skema tanda tangan tidak dikenal: {self.scheme!r}")
        if self.scheme != DETACHED_ISO and not self.checksum_url:
            problems.append("URL file checksum kosong")
        if self.scheme != CLEARSIGNED_CHECKSUM and not self.signature_url:
            problems.append("URL tanda tangan kosong")
        if self.scheme == DETACHED_ISO and not self.sha256:
            problems.append("SHA256 dari feed kosong")
        for url in (self.iso_url, self.checksum_url, self.signature_url):
            if url is not None and not url.startswith("https://"):
                problems.append(f"bukan HTTPS: {url}")
        return problems


# Nama file dari feed/listing dipakai sebagai nama file lokal, jadi tidak boleh berisi path.
_SAFE_NAME = re.compile(r"^[\w][\w.+-]*$")


def _basename(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


@dataclass
class SavedRelease:
    release: Release
    folder: Path
    verified: dict | None  # isi verified.json, None kalau belum pernah lolos

    @property
    def iso_path(self) -> Path:
        return self.folder / self.release.iso_name


def release_folder(release: Release, root: Path | None = None) -> Path:
    return (root or iso_dir()) / release.distro / release.folder_name


def save_release(release: Release, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / RELEASE_FILE).write_text(json.dumps(release.to_dict(), indent=2) + "\n", encoding="utf-8")


def saved_releases(distro: str | None = None, root: Path | None = None) -> list[SavedRelease]:
    """Unduhan yang tercatat, terbaru dulu per distro."""
    root = root or iso_dir()
    found = []
    for path in sorted(root.glob(f"{distro or '*'}/*/{RELEASE_FILE}")):
        try:
            release = Release.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, IsoError):
            continue
        verified = None
        try:
            verified = json.loads((path.parent / VERIFIED_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        found.append(SavedRelease(release, path.parent, verified))
    found.sort(key=lambda s: (s.release.distro, version_key(s.release.version)), reverse=True)
    return found
