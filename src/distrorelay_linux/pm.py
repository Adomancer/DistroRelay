"""Perintah package manager per keluarga distro, repo tambahan, Flatpak, dan AUR.

Modul ini hanya MENYUSUN perintah; yang menjalankannya provision.py lewat Runner. Semua perintah memakai argumen
terpisah (tanpa shell), dan nama paket sudah divalidasi oleh profil.
"""

from __future__ import annotations

import re
from typing import Sequence

from .runner import Command, as_root, command

FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
RPMFUSION_BASE = "https://mirrors.rpmfusion.org"
PARU_AUR_URL = "https://aur.archlinux.org/paru-bin.git"
PACMAN_CONF = "/etc/pacman.conf"


class Backend:
    family = ""
    name = ""

    def refresh(self, is_root: bool) -> list[Command]:
        return []

    def install(self, packages: Sequence[str], is_root: bool) -> Command:
        raise NotImplementedError

    def prepare_each(self, is_root: bool) -> list[Command]:
        """Dijalankan sekali sebelum paket dicoba satu per satu (setelah pemasangan sekaligus gagal)."""
        return []

    def install_one(self, packages: Sequence[str], is_root: bool) -> Command:
        """Perintah per paket di fallback satu per satu."""
        return self.install(packages, is_root)


class Dnf(Backend):
    family, name = "fedora", "dnf"

    def install(self, packages, is_root):
        return as_root(["dnf", "install", "-y", *packages], is_root)


class Pacman(Backend):
    family, name = "arch", "pacman"

    def install(self, packages, is_root):
        # -Syu sekaligus: memasang dengan -Sy tanpa -u = "partial upgrade", tidak didukung Arch.
        return as_root(["pacman", "-Syu", "--needed", "--noconfirm", *packages], is_root)

    def prepare_each(self, is_root):
        # Sinkron + upgrade sekali; sesudahnya -S per paket aman dari partial upgrade (sistem sudah terbaru).
        return [as_root(["pacman", "-Syu", "--noconfirm"], is_root)]

    def install_one(self, packages, is_root):
        return as_root(["pacman", "-S", "--needed", "--noconfirm", *packages], is_root)


class Apt(Backend):
    family, name = "debian", "apt"

    def refresh(self, is_root):
        return [as_root(["apt-get", "update"], is_root)]

    def install(self, packages, is_root):
        # --no-remove: dengan -y, apt menyelesaikan konflik dengan MENGHAPUS paket lain tanpa bertanya
        # (mis. pipewire-pulse menghapus pulseaudio). Setup hanya boleh menambah, jadi lebih baik gagal.
        return as_root(["apt-get", "install", "-y", "--no-remove", *packages], is_root,
                       env={"DEBIAN_FRONTEND": "noninteractive"})


_BACKENDS = {b.family: b for b in (Dnf(), Pacman(), Apt())}


def backend_for(family: str) -> Backend:
    return _BACKENDS[family]


# --- Repo tambahan -----------------------------------------------------------------


def rpmfusion(version_id: str, is_root: bool) -> Command:
    if not re.match(r"^[0-9]+$", version_id or ""):
        raise ValueError(f"Versi Fedora {version_id!r} tidak dikenali (butuh angka, mis. 41)")
    urls = [
        f"{RPMFUSION_BASE}/free/fedora/rpmfusion-free-release-{version_id}.noarch.rpm",
        f"{RPMFUSION_BASE}/nonfree/fedora/rpmfusion-nonfree-release-{version_id}.noarch.rpm",
    ]
    return as_root(["dnf", "install", "-y", *urls], is_root)


def universe(is_root: bool) -> list[Command]:
    return [as_root(["add-apt-repository", "-y", "universe"], is_root), as_root(["apt-get", "update"], is_root)]


def enable_multilib(conf: str) -> str | None:
    """Teks pacman.conf dengan [multilib] diaktifkan; None kalau sudah aktif. ValueError kalau bagiannya tidak ada."""
    lines = conf.splitlines(keepends=True)
    for n, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[multilib]":
            return None
        if stripped.replace(" ", "") == "#[multilib]":
            lines[n] = "[multilib]\n"
            following = lines[n + 1] if n + 1 < len(lines) else ""
            if re.match(r"^\s*#\s*Include\s*=", following):
                lines[n + 1] = following.split("#", 1)[1].lstrip()
            return "".join(lines)
    raise ValueError(f"Bagian [multilib] tidak ditemukan di {PACMAN_CONF}")


def install_file(source: str, target: str, backup: str, is_root: bool) -> list[Command]:
    """Ganti file milik root: cadangkan dulu, lalu pasang salinan baru dengan mode 644."""
    return [as_root(["cp", "-p", target, backup], is_root), as_root(["install", "-m", "644", source, target], is_root)]


# --- Flatpak --------------------------------------------------------------------------


def flathub_remote(is_root: bool) -> Command:
    return as_root(["flatpak", "remote-add", "--if-not-exists", "flathub", FLATHUB_URL], is_root)


def flatpak_install(app_ids: Sequence[str], is_root: bool) -> Command:
    return as_root(["flatpak", "install", "-y", "--noninteractive", "flathub", *app_ids], is_root)


# --- AUR (lewat paru) --------------------------------------------------------------------


def paru_bootstrap(workdir: str, is_root: bool) -> list[Command]:
    """Bangun paru-bin dari AUR. makepkg menolak jalan sebagai root, jadi ini selalu sebagai user biasa."""
    return [
        as_root(["pacman", "-Syu", "--needed", "--noconfirm", "base-devel", "git"], is_root),
        command("git", "clone", "--depth", "1", PARU_AUR_URL, f"{workdir}/paru-bin"),
        command("makepkg", "-si", "--noconfirm", cwd=f"{workdir}/paru-bin"),
    ]


def aur_install(packages: Sequence[str]) -> Command:
    return command("paru", "-S", "--needed", "--noconfirm", *packages)
