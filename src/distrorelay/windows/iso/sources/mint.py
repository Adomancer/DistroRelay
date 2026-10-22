"""Linux Mint: tidak punya feed, jadi membaca listing folder `stable/` di mirror resmi; lalu `sha256sum.txt` +
`sha256sum.txt.gpg` di folder versinya.

Kunci: "Linux Mint ISO Signing Key", dari keyserver.ubuntu.com, dicocokkan dengan panduan verifikasi resmi
https://linuxmint-installation-guide.readthedocs.io/en/latest/verify.html (2026-10-22).
"""

from __future__ import annotations

import re

from .. import DETACHED_CHECKSUM, IsoError, KeySpec, Release, version_key
from ..verify import parse_checksums

NAME = "Linux Mint"
BASE = "https://mirrors.kernel.org/linuxmint/stable/"
KEYS = KeySpec.bundled("mint.gpg", {"27DEB15644C6B3CF3BD7D291300F846BA25BAE09"})
EDITIONS = ("cinnamon", "mate", "xfce")
DEFAULT_EDITION = "cinnamon"
LOCAL_PATTERN = re.compile(
    r"^linuxmint-(?P<version>\d+(?:\.\d+)?)-(?P<edition>cinnamon|mate|xfce)-64bit\.iso$", re.IGNORECASE
)
_VERSION_HREF = re.compile(r'href="(\d+(?:\.\d+)?)/"')


def newest_version(listing: str) -> str:
    versions = _VERSION_HREF.findall(listing)
    if not versions:
        raise IsoError("listing mirror Mint tidak berisi folder versi")
    return max(versions, key=version_key)


def latest(fetch, edition: str | None = None) -> Release:
    edition = (edition or DEFAULT_EDITION).lower()
    if edition not in EDITIONS:
        raise IsoError(f"edisi Mint {edition!r} tidak dikenal (pilihan: {', '.join(EDITIONS)})")
    version = newest_version(fetch(BASE).decode("utf-8", "replace"))
    folder = f"{BASE}{version}/"
    iso_name = f"linuxmint-{version}-{edition}-64bit.iso"
    if iso_name not in parse_checksums(fetch(folder + "sha256sum.txt").decode("utf-8", "replace")):
        raise IsoError(f"{iso_name} tidak tercantum di sha256sum.txt Mint {version}")
    return Release(
        distro="mint",
        version=version,
        edition=edition,
        iso_name=iso_name,
        iso_url=folder + iso_name,
        scheme=DETACHED_CHECKSUM,
        checksum_url=folder + "sha256sum.txt",
        signature_url=folder + "sha256sum.txt.gpg",
    )
