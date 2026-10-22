"""Arch Linux: feed `releng/releases/json`; ISO ditandatangani langsung (`.iso.sig`) oleh release engineer.

Arch tidak punya file checksum bertanda tangan, jadi lapis kedua adalah SHA256 dari feed resmi.
Kunci: Pierre Schmitz, dari keyserver.ubuntu.com, dicocokkan dengan WKD archlinux.org dan halaman
https://archlinux.org/download/ (2026-10-22).
"""

from __future__ import annotations

import json
import re

from .. import DETACHED_ISO, IsoError, KeySpec, Release

NAME = "Arch Linux"
FEED = "https://archlinux.org/releng/releases/json/"
MIRROR = "https://geo.mirror.pkgbuild.com"
KEYS = KeySpec.bundled(
    "arch.gpg",
    {
        "3E80CA1A8B89F69CBA57D98A76A5EF9054449A5C",  # Pierre Schmitz <pierre@archlinux.org>
        "4AA4767BBC9C4B1D18AE28B77F2D434B9741E8AC",  # Pierre Schmitz (kunci lama)
    },
)
EDITIONS = ("x86_64",)
DEFAULT_EDITION = "x86_64"
LOCAL_PATTERN = re.compile(r"^archlinux-(?P<version>\d{4}\.\d{2}\.\d{2})-x86_64\.iso$", re.IGNORECASE)
_ISO_PATH = re.compile(r"^/iso/[\d.]+/archlinux-[\d.]+-x86_64\.iso$")


def pick(feed: dict) -> dict:
    latest_version = feed.get("latest_version")
    for release in feed.get("releases", []):
        if release.get("version") == latest_version and release.get("available"):
            return release
    raise IsoError(f"rilis terbaru Arch ({latest_version}) tidak tersedia di feed")


def latest(fetch, edition: str | None = None) -> Release:
    if edition not in (None, DEFAULT_EDITION):
        raise IsoError("Arch hanya punya satu ISO (x86_64)")
    try:
        release = pick(json.loads(fetch(FEED)))
        iso_path = release["iso_url"]
        sha256 = release["sha256_sum"]
    except (ValueError, TypeError, KeyError) as exc:
        raise IsoError(f"format feed Arch berubah: {exc}") from exc
    if not _ISO_PATH.match(iso_path or "") or not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise IsoError("format feed Arch berubah (path ISO atau SHA256 tidak dikenali)")
    iso_url = MIRROR + iso_path
    size = (release.get("torrent") or {}).get("file_length")
    return Release(
        distro="arch",
        version=release["version"],
        edition=DEFAULT_EDITION,
        iso_name=iso_path.rsplit("/", 1)[1],
        iso_url=iso_url,
        scheme=DETACHED_ISO,
        signature_url=iso_url + ".sig",
        size=size if isinstance(size, int) else None,
        sha256=sha256,
    )
