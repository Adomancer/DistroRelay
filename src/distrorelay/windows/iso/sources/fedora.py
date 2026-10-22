"""Fedora: feed `releases.json`; file `*-CHECKSUM` di folder ISO ditandatangani inline (clearsigned).

Kunci: `fedora.gpg` dari https://fedoraproject.org/fedora.gpg, dicocokkan dengan
`RPM-GPG-KEY-fedora-<N>-primary` di src.fedoraproject.org/rpms/fedora-repos (2026-10-22). Fedora memakai kunci
baru per rilis; rilis yang kuncinya belum ada di sini ditolak sampai DistroRelay diperbarui.
"""

from __future__ import annotations

import json
import re

from .. import CLEARSIGNED_CHECKSUM, IsoError, KeySpec, Release

NAME = "Fedora"
FEED = "https://fedoraproject.org/releases.json"
KEYS = KeySpec.bundled(
    "fedora.gpg",
    {
        "C6E7F081CF80E13146676E88829B606631645531",  # Fedora (43)
        "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6",  # Fedora (44)
        "4F50A6114CD5C6976A7F1179655A4B02F577861E",  # Fedora (45)
        "D924B10D3E810DABDD8B56B596E7E91491211FCE",  # Fedora (46)
    },
)
EDITIONS = ("Workstation", "KDE", "Xfce", "Cinnamon", "Budgie", "LXQt", "Sway", "i3", "COSMIC")
DEFAULT_EDITION = "Workstation"
LOCAL_PATTERN = re.compile(
    r"^Fedora-(?P<edition>[\w-]+?)-Live-(?:x86_64-)?(?P<version>\d+)-[\d.]+(?:\.x86_64)?\.iso$", re.IGNORECASE
)
_CHECKSUM_HREF = re.compile(r'href="([\w.+-]+-CHECKSUM)"')


def pick(feed: list, edition: str) -> dict:
    """Entri ISO x86_64 rilis stabil terbaru (bukan Beta) untuk edisi itu."""
    wanted = edition.lower()
    candidates = [
        entry
        for entry in feed
        if isinstance(entry, dict)
        and entry.get("arch") == "x86_64"
        and str(entry.get("version", "")).isdigit()
        and str(entry.get("link", "")).endswith(".iso")
        and wanted in (str(entry.get("subvariant", "")).lower(), str(entry.get("variant", "")).lower())
    ]
    if not candidates:
        raise IsoError(f"edisi Fedora {edition!r} tidak ada di feed")
    newest = max(int(entry["version"]) for entry in candidates)
    newest_entries = [e for e in candidates if int(e["version"]) == newest]
    # Workstation/Server bisa punya beberapa ISO (Live, dvd, netinst); Live dulu.
    newest_entries.sort(key=lambda e: "-Live-" not in e["link"])
    return newest_entries[0]


def checksum_name(listing: str, iso_name: str) -> str:
    names = sorted(set(_CHECKSUM_HREF.findall(listing)))
    if not names:
        raise IsoError("file CHECKSUM tidak ditemukan di folder ISO Fedora")
    prefix = iso_name.split("-Live-")[0]
    return next((n for n in names if n.startswith(prefix + "-")), names[0])


def latest(fetch, edition: str | None = None) -> Release:
    try:
        entry = pick(json.loads(fetch(FEED)), edition or DEFAULT_EDITION)
    except (ValueError, TypeError, KeyError) as exc:
        raise IsoError(f"format releases.json Fedora berubah: {exc}") from exc
    link = entry["link"]
    folder, iso_name = link.rsplit("/", 1)
    listing = fetch(folder + "/").decode("utf-8", "replace")
    checksum = checksum_name(listing, iso_name)
    size = entry.get("size")
    return Release(
        distro="fedora",
        version=str(entry["version"]),
        edition=str(entry.get("subvariant") or edition or DEFAULT_EDITION),
        iso_name=iso_name,
        iso_url=link,
        scheme=CLEARSIGNED_CHECKSUM,
        checksum_url=f"{folder}/{checksum}",
        size=int(size) if str(size).isdigit() else None,
        sha256=entry.get("sha256"),
    )
