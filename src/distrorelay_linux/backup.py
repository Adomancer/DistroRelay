"""`backup` di distro lama (M5): daftar paket yang dikurasi, dotfiles, dan file sensitif terenkripsi.

Hasilnya satu folder di <drive>/DistroRelay/linux-backups/<waktu>-<distro>/:
    manifest.json            distro, UUID filesystem / dan /home (+ LUKS), daftar paket mentah, `profile_update`,
                             dan SHA256 setiap file
    dotfiles.tar.gz          dotfiles.paths dari profil
    sensitive.tar.gz.gpg     dotfiles.sensitive, dienkripsi gpg (AES256, passphrase). HANYA di USB: kalau backup ini
                             tidak di USB, file sensitif ditaruh di kit lain yang ada di USB, atau tidak dibackup sama
                             sekali. Isinya tidak pernah ditulis ke disk tanpa enkripsi.

Backup ini BUKAN pengganti backup data pribadi (dokumen/foto/kode): pakai rsync, restic, atau Déjà Dup untuk itu.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from distrorelay.common.profile import home_relative

from . import dotfiles
from .distro import Distro
from .kit import sha256_file
from .runner import command

BACKUP_FORMAT = 1
MANIFEST = "manifest.json"
DOTFILES = "dotfiles.tar.gz"
SENSITIVE = "sensitive.tar.gz.gpg"
_BASE_DIR = Path(__file__).resolve().parent.parent / "distrorelay" / "common" / "data" / "base_packages"
# OpenPGP: paket pertama file terenkripsi simetris = Symmetric-Key Encrypted Session Key (tag 3).
_SKESK_HEADERS = (b"\x8c", b"\x8d", b"\xc3")
# Arsip file sensitif dibuat di memori (isi rahasia tidak pernah ditulis tanpa enkripsi), jadi ukurannya dibatasi.
SENSITIVE_LIMIT = 256 * 1024 * 1024


class BackupError(RuntimeError):
    pass


# --- Daftar paket ----------------------------------------------------------------------


@dataclass
class Installed:
    native: list[str] = field(default_factory=list)
    aur: list[str] = field(default_factory=list)
    flatpak: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    core: list[str] = field(default_factory=list)  # debian: paket inti / pustaka menurut metadata dpkg, tidak dibawa


# debootstrap (WSL, image cloud, sebagian installer) menandai semua paket Priority required/important sebagai "manual",
# jadi `apt-mark showmanual` saja ikut membawa libc6, init, udev, dst. (uji laptop 2026-10-25, temuan T-7).
_DEBIAN_CORE_PRIORITIES = {"required", "important"}
_DEBIAN_LIB_SECTIONS = {"libs", "oldlibs"}


def _debian_core(runner, problems: list[str]) -> set:
    """Paket inti sistem (Essential, Priority required/important) dan pustaka (Section libs) menurut metadata dpkg."""
    result = runner.run(command("dpkg-query", "-W", "-f=${Package}\t${Essential}\t${Priority}\t${Section}\n"))
    if not result.ok:
        problems.append(f"`dpkg-query` gagal: {result.output() or result.returncode}; paket inti sistem tidak bisa disaring")
        return set()
    core = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        name, essential, priority, section = (f.strip() for f in fields)
        if (essential == "yes" or priority in _DEBIAN_CORE_PRIORITIES
                or section.rsplit("/", 1)[-1] in _DEBIAN_LIB_SECTIONS):  # Ubuntu: "universe/libs"
            core.add(name)
    return core


def _lines(text: str) -> list[str]:
    seen, result = set(), []
    for line in text.splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def installed_packages(runner, distro: Distro) -> Installed:
    """Paket yang dipasang pengguna (bukan dependensi), per package manager distro ini."""
    found = Installed()

    def query(*args: str) -> list[str]:
        result = runner.run(command(*args))
        if not result.ok:
            found.problems.append(f"`{' '.join(args)}` gagal: {result.output() or result.returncode}")
            return []
        return _lines(result.stdout)

    if distro.family == "fedora":
        found.native = query("dnf", "repoquery", "--userinstalled", "--queryformat", "%{name}\n")
    elif distro.family == "arch":
        explicit = query("pacman", "-Qqe")
        found.aur = query("pacman", "-Qqem")  # "foreign": tidak dari repo resmi, umumnya dari AUR
        found.native = [p for p in explicit if p not in set(found.aur)]
    elif distro.family == "debian":
        manual = query("apt-mark", "showmanual")
        core = _debian_core(runner, found.problems) if manual else set()
        # apt-mark menulis paket arsitektur lain sebagai nama:arch; dpkg-query ${Package} tanpa :arch.
        found.core = [p for p in manual if p.split(":", 1)[0] in core]
        found.native = [p for p in manual if p.split(":", 1)[0] not in core]
    if runner.which("flatpak"):
        found.flatpak = query("flatpak", "list", "--app", "--columns=application")
    return found


def base_patterns(family: str, base_dir: Path = _BASE_DIR) -> list[str]:
    try:
        text = (base_dir / f"{family}.txt").read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def filter_packages(names: list[str], patterns: list[str], exclude: list[str]) -> tuple[list[str], list[str]]:
    """(dibawa, disaring): paket bawaan distro (pola) dan hasil kurasi sebelumnya (`exclude`) tidak dibawa."""
    kept, removed = [], []
    for name in names:
        if name in exclude or any(fnmatch.fnmatchcase(name, p) for p in patterns):
            removed.append(name)
        else:
            kept.append(name)
    return kept, removed


def parse_selection(answer: str, count: int) -> set[int] | None:
    """'3 5 8-10' -> {3, 5, 8, 9, 10} (nomor mulai 1). Kosong = tidak ada. Tidak valid = None."""
    chosen = set()
    for token in answer.replace(",", " ").split():
        start, sep, end = token.partition("-")
        if not start.isdigit() or (sep and not end.isdigit()):
            return None
        first, last = int(start), int(end) if sep else int(start)
        if not 1 <= first <= last <= count:
            return None
        chosen.update(range(first, last + 1))
    return chosen


def profile_update(native: list[str], aur: list[str], flatpak: list[str], excluded: list[str], package_map,
                   distro: Distro) -> dict:
    """Bagian yang nanti digabung ke profil di Windows (`distrorelay profile import`)."""
    logical, unmapped = package_map.reverse(native, distro.id, distro.family)
    return {
        "native": logical,
        "distro_specific": {distro.family: unmapped} if unmapped else {},
        "aur": list(aur),
        "flatpak": list(flatpak),
        "exclude": list(excluded),
    }


# --- Filesystem & USB ------------------------------------------------------------------


def _json(runner, *args: str) -> dict:
    result = runner.run(command(*args))
    if not result.ok:
        return {}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {}


def _walk(devices: list):
    for device in devices or []:
        yield device
        yield from _walk(device.get("children", []))


def filesystems(runner) -> dict:
    """UUID filesystem / dan /home, plus UUID LUKS kalau terenkripsi. Dicocokkan Windows sebelum hapus partisi (M3)."""
    found = {}
    for target in ("/", "/home"):
        data = _json(runner, "findmnt", "-J", "-o", "SOURCE,UUID,FSTYPE,TARGET", "--target", target)
        entries = data.get("filesystems") or []
        if not entries:
            continue
        entry = entries[0]
        source = str(entry.get("source") or "").split("[", 1)[0]  # btrfs: /dev/x[/subvol]
        info = {"mountpoint": entry.get("target"), "uuid": entry.get("uuid"), "fstype": entry.get("fstype"),
                "source": source, "luks_uuid": None}
        if source.startswith("/dev/"):
            tree = _json(runner, "lsblk", "-J", "-s", "-o", "NAME,UUID,FSTYPE", source)
            luks = [d for d in _walk(tree.get("blockdevices", [])) if d.get("fstype") == "crypto_LUKS"]
            info["luks_uuid"] = luks[0].get("uuid") if luks else None
        found[target] = info
    return found


def on_usb(runner, path: Path) -> bool:
    """Apakah `path` ada di drive USB (lsblk TRAN=usb di salah satu induk perangkatnya)."""
    data = _json(runner, "findmnt", "-J", "-o", "SOURCE", "--target", str(path))
    entries = data.get("filesystems") or []
    source = str(entries[0].get("source") or "").split("[", 1)[0] if entries else ""
    if not source.startswith("/dev/"):
        return False
    tree = _json(runner, "lsblk", "-J", "-s", "-o", "NAME,TRAN", source)
    return any(d.get("tran") == "usb" for d in _walk(tree.get("blockdevices", [])))


# --- Membuat backup -------------------------------------------------------------------------


@dataclass
class BackupResult:
    path: Path
    manifest: dict
    notes: list[str] = field(default_factory=list)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gpg_env() -> dict:
    """pinentry di terminal butuh GPG_TTY; stdin dipakai untuk data, jadi passphrase diminta lewat tty."""
    try:
        return {"GPG_TTY": os.ttyname(sys.stderr.fileno())}
    except (OSError, AttributeError):
        return {}


def encrypt(runner, data: bytes) -> bytes:
    result = runner.run_bytes(
        command("gpg", "--symmetric", "--cipher-algo", "AES256", "--no-armor", "--output", "-", env=_gpg_env()), data
    )
    if not result.ok or not result.data[:1] in _SKESK_HEADERS:
        raise BackupError(f"gpg gagal mengenkripsi (kode {result.returncode})")
    return result.data


def decrypt(runner, path: Path) -> bytes:
    result = runner.run_bytes(command("gpg", "--decrypt", str(path), env=_gpg_env()))
    if not result.ok:
        raise BackupError(f"gpg gagal mendekripsi {path} (passphrase salah?)")
    return result.data


def create_backup(
    kit,
    runner,
    distro: Distro,
    *,
    home: Path,
    curate: Callable[[list[str]], list[str]],
    usb_backup_dirs: Callable[[], list[Path]] = lambda: [],
    now: datetime | None = None,
    include_sensitive: bool = True,
) -> BackupResult:
    """`curate(kandidat)` -> nama yang TIDAK dibawa. `usb_backup_dirs()` -> folder linux-backups lain di USB."""
    if distro.family is None:
        raise BackupError(f"Distro {distro.describe} belum didukung")
    now = now or datetime.now()
    backup_id = f"{now:%Y%m%d-%H%M%S}-{distro.id}"
    final = kit.backups_dir / backup_id
    staging = kit.backups_dir / f".{backup_id}.tmp"
    notes: list[str] = []
    profile = kit.profile

    installed = installed_packages(runner, distro)
    notes += installed.problems
    patterns = base_patterns(distro.family)
    native, auto_removed = filter_packages(installed.native, patterns, profile.exclude)
    aur, aur_removed = filter_packages(installed.aur, [], profile.exclude)
    if installed.core:
        notes.append(f"{len(installed.core)} paket inti sistem / pustaka tidak dibawa (Priority/Section dari dpkg)")
    if auto_removed:
        notes.append(f"{len(auto_removed)} paket sistem/hasil kurasi lama tidak dibawa")
    excluded = curate(native + aur)
    native = [p for p in native if p not in excluded]
    aur = [p for p in aur if p not in excluded]
    update = profile_update(native, aur, installed.flatpak, excluded, kit.packages, distro)

    staging.mkdir(parents=True, exist_ok=False)
    files, sensitive_info = [], None
    try:
        if profile.dotfiles:
            with open(staging / DOTFILES, "wb") as handle:
                report = dotfiles.write_archive(
                    handle, home, [home_relative(p) for p in profile.dotfiles],
                    exclude=[home_relative(p) for p in profile.sensitive],
                )
            files.append(DOTFILES)
            notes += [f"Dotfile {rel} tidak dibackup: {why}" for rel, why in report.skipped]
        if profile.sensitive and include_sensitive:
            sensitive_info = _backup_sensitive(runner, kit, staging, backup_id, home, usb_backup_dirs, notes)
            if sensitive_info and sensitive_info["location"] == "here":
                files.append(SENSITIVE)
        manifest = {
            "format": BACKUP_FORMAT,
            "backup_id": backup_id,
            "created_at": now.astimezone().isoformat(timespec="seconds"),
            "distro": {"id": distro.id, "family": distro.family, "version_id": distro.version_id,
                       "pretty_name": distro.pretty_name},
            "filesystems": filesystems(runner),
            "packages": {"native": installed.native, "aur": installed.aur, "flatpak": installed.flatpak,
                         "not_carried": sorted(set(auto_removed + aur_removed + excluded))},
            "profile_update": update,
            "sensitive": sensitive_info,
            "files": [{"path": n, "size": (staging / n).stat().st_size, "sha256": sha256_file(staging / n)}
                      for n in files],
            "notes": notes,
            "warning": "Bukan backup dokumen/foto/kode: hanya daftar paket dan dotfiles.",
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        problems = verify(staging)
        if problems:
            raise BackupError("Backup tidak cocok setelah ditulis: " + "; ".join(problems[:3]))
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if sensitive_info and sensitive_info["location"] != "here":
            # Salinan terenkripsi di kit USB lain milik backup yang batal ini: jangan ditinggal yatim.
            elsewhere = Path(sensitive_info["location"])
            (elsewhere / SENSITIVE).unlink(missing_ok=True)
            try:
                elsewhere.rmdir()
            except OSError:
                pass
        raise
    return BackupResult(final, manifest, notes)


def _backup_sensitive(runner, kit, staging: Path, backup_id: str, home: Path, usb_backup_dirs, notes) -> dict | None:
    if not runner.which("gpg"):
        notes.append("File sensitif TIDAK dibackup: gpg tidak ada")
        return None
    if on_usb(runner, kit.backups_dir.parent):
        target_dir, location = staging, "here"
    else:
        others = [d for d in usb_backup_dirs() if d != kit.backups_dir]
        if not others:
            notes.append("File sensitif TIDAK dibackup: kit ini tidak di USB dan tidak ada kit lain di USB "
                         "(file sensitif selalu disimpan di USB)")
            return None
        target_dir, location = others[0] / backup_id, str(others[0] / backup_id)
    buffer = io.BytesIO()  # isi rahasia hanya di memori; yang ditulis ke disk hanya hasil enkripsi
    report = dotfiles.write_archive(buffer, home, [home_relative(p) for p in kit.profile.sensitive],
                                    follow_symlinks=True, max_total_size=SENSITIVE_LIMIT)
    notes += [f"File sensitif {rel} tidak dibackup: {why}" for rel, why in report.skipped]
    notes += [f"File sensitif {rel} adalah symlink; isi {target} yang dienkripsi" for rel, target in report.followed]
    try:
        encrypted = encrypt(runner, buffer.getvalue())
    except BackupError as exc:  # mis. prompt passphrase dibatalkan: sisa backup tetap jalan
        notes.append(f"File sensitif TIDAK dibackup: {exc}")
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / SENSITIVE).write_bytes(encrypted)
    return {"location": location, "file": SENSITIVE, "sha256": sha256_bytes(encrypted), "count": len(report.files)}


# --- Membaca backup (Linux setup & Windows) ----------------------------------------------------


def read_manifest(path: Path) -> dict:
    try:
        manifest = json.loads((Path(path) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{MANIFEST} tidak bisa dibaca: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise BackupError("Format backup tidak didukung")
    return manifest


def verify(path: Path, *, hashes: bool = True) -> list[str]:
    """Masalah integritas backup (kosong = cocok dengan manifest). `hashes=False` = cek cepat: file ada dan ukurannya
    sama, tanpa membaca isinya (cukup untuk info, bukan untuk memakai isinya)."""
    path = Path(path)
    try:
        manifest = read_manifest(path)
    except BackupError as exc:
        return [str(exc)]
    problems = []
    for entry in manifest.get("files", []):
        name = str(entry.get("path", ""))
        if name not in (DOTFILES, SENSITIVE):
            problems.append(f"{name!r}: nama file tidak dikenal di manifest")
        elif not (path / name).is_file():
            problems.append(f"{name}: hilang")
        elif not hashes:
            if (path / name).stat().st_size != entry.get("size"):
                problems.append(f"{name}: ukurannya berubah")
        elif sha256_file(path / name) != entry.get("sha256"):
            problems.append(f"{name}: isinya berubah (hash tidak cocok)")
    listed = {str(entry.get("path", "")) for entry in manifest.get("files", [])}
    for name in (DOTFILES, SENSITIVE):
        if (path / name).exists() and name not in listed:
            problems.append(f"{name}: ada di folder tapi tidak tercatat di manifest")
    return problems


def iter_backups(backups_dir: Path, *, hashes: bool = True) -> Iterator[tuple[Path, dict | None, list[str]]]:
    """Backup di folder itu satu per satu, terbaru dulu: (folder, manifest atau None, masalah verifikasi).
    Diverifikasi saat diambil, jadi pemanggil yang berhenti di backup pertama tidak membaca sisanya."""
    try:
        folders = sorted((p for p in Path(backups_dir).iterdir() if p.is_dir() and not p.name.startswith(".")),
                         reverse=True)
    except OSError:
        return
    for folder in folders:
        if not (folder / MANIFEST).exists() and (folder / SENSITIVE).exists():
            continue  # salinan file sensitif milik backup di kit lain (kit itu tidak di USB), bukan backup sendiri
        try:
            manifest = read_manifest(folder)
        except BackupError as exc:
            yield folder, None, [str(exc)]
            continue
        yield folder, manifest, verify(folder, hashes=hashes)


def list_backups(backups_dir: Path, *, hashes: bool = True) -> list[tuple[Path, dict | None, list[str]]]:
    """Semua backup di folder itu (terbaru dulu): (folder, manifest atau None, masalah verifikasi)."""
    return list(iter_backups(backups_dir, hashes=hashes))


def latest_backup(backups_dir: Path, skipped: list | None = None) -> Path | None:
    """Backup terbaru yang lolos verifikasi. Backup yang lebih lama tidak dibaca. `skipped` (kalau diberi) diisi
    (folder, masalah) untuk backup lebih baru yang rusak dan dilewati, supaya setup bisa menyebutnya."""
    for folder, manifest, problems in iter_backups(backups_dir):
        if manifest and not problems:
            return folder
        if skipped is not None:
            skipped.append((folder, problems))
    return None


def sensitive_path(folder: Path, manifest: dict, search_dirs: list[Path]) -> Path | None:
    """Lokasi sensitive.tar.gz.gpg milik backup ini: di foldernya sendiri, atau di folder backup senama di USB lain."""
    info = manifest.get("sensitive") or {}
    if not info:
        return None
    candidates = [Path(folder) / SENSITIVE] + [Path(d) / manifest.get("backup_id", "") / SENSITIVE for d in search_dirs]
    for candidate in candidates:
        if candidate.is_file() and sha256_file(candidate) == info.get("sha256"):
            return candidate
    return None


def covered_uuids(manifest: dict) -> set[str]:
    """UUID filesystem + LUKS yang tercatat di backup ini (huruf kecil)."""
    found = set()
    for info in (manifest.get("filesystems") or {}).values():
        for key in ("uuid", "luks_uuid"):
            if info.get(key):
                found.add(str(info[key]).lower())
    return found
