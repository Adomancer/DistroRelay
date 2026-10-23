"""Ubuntu (LTS, desktop): feed `meta-release-lts`; `SHA256SUMS` + `SHA256SUMS.gpg` di releases.ubuntu.com.

Nama ISO diambil dari SHA256SUMS (rilis titik seperti 26.04.1 bisa muncul belakangan dari versi di feed).
Kunci: "Ubuntu CD Image Automatic Signing Key (2012)" (RSA-4096), dari keyserver.ubuntu.com, dicocokkan dengan
https://ubuntu.com/tutorials/how-to-verify-ubuntu (2026-10-22). Kunci 2004 (DSA-1024, terlalu lemah) tidak dipakai;
rilis yang masih didukung ditandatangani kunci 2012.
"""

from __future__ import annotations

import re

from .. import DETACHED_CHECKSUM, IsoError, KeySpec, Release, version_key
from ..verify import parse_checksums

NAME = "Ubuntu LTS"
FEED = "https://changelogs.ubuntu.com/meta-release-lts"
BASE = "https://releases.ubuntu.com/{dist}/"
KEYS = KeySpec.bundled(
    "ubuntu.gpg",
    {
        "843938DF228D22F7B3742BC0D94AA3F0EFE21092",  # Ubuntu CD Image Automatic Signing Key (2012)
    },
)
EDITIONS = ("desktop",)
DEFAULT_EDITION = "desktop"
LOCAL_PATTERN = re.compile(r"^ubuntu-(?P<version>\d+\.\d+(?:\.\d+)?)-desktop-amd64\.iso$", re.IGNORECASE)
_DIST = re.compile(r"^[a-z]+$")


def parse_meta_release(text: str) -> list[dict[str, str]]:
    entries, current = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
        elif ":" in line:
            key, value = line.split(":", 1)
            current[key.strip()] = value.strip()
    return entries


def newest_supported(text: str) -> dict[str, str]:
    supported = [e for e in parse_meta_release(text) if e.get("Supported") == "1" and _DIST.match(e.get("Dist", ""))]
    if not supported:
        raise IsoError("tidak ada rilis LTS yang didukung di meta-release-lts")
    return max(supported, key=lambda e: version_key(e.get("Version", "0")))


def latest(fetch, edition: str | None = None) -> Release:
    if edition not in (None, DEFAULT_EDITION):
        raise IsoError("hanya Ubuntu desktop yang didukung")
    entry = newest_supported(fetch(FEED).decode("utf-8", "replace"))
    base = BASE.format(dist=entry["Dist"])
    sums = parse_checksums(fetch(base + "SHA256SUMS").decode("utf-8", "replace"))
    isos = [(m.group("version"), name) for name in sums if (m := LOCAL_PATTERN.match(name))]
    if not isos:
        raise IsoError(f"ISO desktop tidak tercantum di SHA256SUMS {entry['Dist']}")
    version, iso_name = max(isos, key=lambda item: version_key(item[0]))
    return Release(
        distro="ubuntu",
        version=version,
        edition=DEFAULT_EDITION,
        iso_name=iso_name,
        iso_url=base + iso_name,
        scheme=DETACHED_CHECKSUM,
        checksum_url=base + "SHA256SUMS",
        signature_url=base + "SHA256SUMS.gpg",
    )
