"""Dotfiles: dikemas jadi tar.gz saat backup (M5), dipulihkan saat setup (M4).

Arsip berisi path relatif terhadap home. Pemulihan aman untuk arsip yang tidak dipercaya penuh: path absolut, '..',
symlink, hardlink, dan file perangkat ditolak, dan tidak ada yang ditulis di luar home. File yang sudah ada dengan
isi berbeda di-rename dulu, bukan ditimpa. File sensitif (~/.ssh, ~/.gnupg) dipulihkan dengan permission ketat.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MAX_FILE_SIZE = 100 * 1024 * 1024  # file lebih besar dari ini (cache, image) tidak dibackup
# Total isi arsip. Profil browser di ~/.config bisa bergiga-giga; dotfiles bukan backup data pribadi.
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024


@dataclass
class RestoreReport:
    restored: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (path, nama cadangannya)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, alasan)

    def summary(self) -> str:
        parts = [f"{len(self.restored)} file dipulihkan"]
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} sudah sama")
        if self.renamed:
            parts.append(f"{len(self.renamed)} file lama di-rename ke *.distrorelay-*.bak")
        if self.skipped:
            parts.append(f"{len(self.skipped)} dilewati")
        return ", ".join(parts)


def safe_member_path(name: str) -> str | None:
    """Path relatif yang aman, atau None. 'a/./b' -> 'a/b'; absolut, '..', kosong -> None."""
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or "\x00" in name:
        return None
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        return _digest(path.read_bytes())
    except OSError:
        return None


def _private_mode(rel: str, is_dir: bool) -> int:
    """Permission ketat untuk file sensitif: folder 700, file 600, kunci publik 644 (seperti ssh-keygen)."""
    if is_dir:
        return 0o700
    return 0o644 if rel.endswith(".pub") else 0o600


def _unique_backup_name(target: Path, stamp: str) -> Path:
    """Nama cadangan yang belum dipakai: file .bak lama (mis. dari member arsip yang dobel) tidak pernah ditimpa."""
    candidate = target.with_name(f"{target.name}.distrorelay-{stamp}.bak")
    counter = 1
    while os.path.lexists(candidate):
        counter += 1
        candidate = target.with_name(f"{target.name}.distrorelay-{stamp}-{counter}.bak")
    return candidate


class _Skip(Exception):
    """Member arsip ini dilewati (alasan = pesan)."""


def _prepare_parents(home: Path, rel: str, private: bool) -> None:
    """Buat folder induk di bawah home satu per satu. Symlink di tengah path ditolak (tidak diikuti), dan folder
    yang baru dibuat untuk file sensitif langsung 700 (bukan mengikuti umask)."""
    current = home
    home.mkdir(parents=True, exist_ok=True)
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise _Skip("ada symlink di path tujuan, tidak diikuti")
        if not current.exists():
            current.mkdir(mode=0o700 if private else 0o777)
            if private:
                os.chmod(current, 0o700)
        elif not current.is_dir():
            raise _Skip("ada file biasa di path tujuan yang seharusnya folder")


def restore(source, home: Path, stamp: str, *, private: bool = False) -> RestoreReport:
    """`source` = path arsip tar.gz, atau isinya (bytes, mis. hasil gpg --decrypt yang tidak pernah ditulis ke disk).

    Satu member yang bermasalah dicatat sebagai dilewati; member lain tetap dipulihkan.
    """
    report = RestoreReport()
    home = Path(home)
    opened = tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") if isinstance(source, bytes) else tarfile.open(source, "r:gz")
    with opened as tar:
        for member in tar.getmembers():
            rel = safe_member_path(member.name)
            if rel is None:
                report.skipped.append((member.name, "path di luar home"))
                continue
            try:
                _restore_member(tar, member, rel, home, stamp, private, report)
            except _Skip as reason:
                report.skipped.append((rel, str(reason)))
            except OSError as exc:
                report.skipped.append((rel, f"gagal: {exc.strerror or exc}"))
    return report


def _restore_member(tar, member, rel: str, home: Path, stamp: str, private: bool, report: RestoreReport) -> None:
    target = home / rel
    if member.isdir():
        _prepare_parents(home, rel + "/x", private)  # folder ini sendiri ikut dibuat/dicek seperti induknya
        if private:
            os.chmod(target, _private_mode(rel, True))
        return
    if not member.isfile():
        kind = "symlink" if member.issym() or member.islnk() else "bukan file biasa"
        raise _Skip(f"{kind}, tidak dipulihkan")
    member_file = tar.extractfile(member)
    data = member_file.read() if member_file is not None else b""
    _prepare_parents(home, rel, private)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise _Skip("di home sudah ada symlink/folder dengan nama ini")
    if target.exists():
        if _file_digest(target) == _digest(data):
            if private:  # isi sama, tapi permission lama bisa longgar
                os.chmod(target, _private_mode(rel, False))
            report.unchanged.append(rel)
            return
        backup = _unique_backup_name(target, stamp)
        os.replace(target, backup)
        report.renamed.append((rel, backup.name))
    if private:  # isi rahasia tidak pernah ada di disk dengan permission longgar, walau sesaat
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(str(target), flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(target, _private_mode(rel, False))
    else:
        with open(target, "wb") as handle:
            handle.write(data)
        os.chmod(target, member.mode & 0o777)  # tanpa setuid/setgid/sticky
    report.restored.append(rel)


# --- Backup (M5) ------------------------------------------------------------------------


@dataclass
class ArchiveReport:
    files: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, alasan)
    followed: list[tuple[str, str]] = field(default_factory=list)  # (path symlink, tujuan yang ikut dikemas)
    total_size: int = 0


_IDENTITY_LIMIT = 200_000


def _identity(home: Path, paths) -> tuple[list[str], set[tuple[int, int]]]:
    """Lokasi asli (realpath) dan inode semua file di bawah path-path ini, termasuk yang dicapai lewat symlink di
    mana pun di dalamnya (mis. GNU stow: ~/.ssh/id_ed25519 -> ../dotfiles/ssh/.ssh/id_ed25519)."""
    roots: list[str] = []
    inodes: set[tuple[int, int]] = set()
    pending = [os.path.realpath(home / rel) for rel in paths if os.path.lexists(home / rel)]
    visited: set[str] = set()
    home_real = os.path.realpath(home)
    walked = 0
    while pending:
        real = pending.pop()
        if real in visited:
            continue
        visited.add(real)
        # Symlink ke / atau ke leluhur home (mis. dosdevices/z: milik Wine) bukan isi sensitif; mengikutinya akan
        # mengecualikan seluruh home dan menelusuri seluruh disk.
        if home_real == real or home_real.startswith(real.rstrip(os.sep) + os.sep):
            continue
        roots.append(real)
        if os.path.isfile(real):
            info = os.stat(real)
            inodes.add((info.st_dev, info.st_ino))
            continue
        if not os.path.isdir(real):
            continue
        for root, dirs, names in os.walk(real):  # followlinks=False; symlink ditangani sendiri di bawah
            walked += len(dirs) + len(names)
            if walked > _IDENTITY_LIMIT:  # path sensitif sebesar ini tidak wajar; lokasi aslinya tetap dikecualikan
                break
            for name in dirs + names:
                full = os.path.join(root, name)
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    pending.append(os.path.realpath(full))  # tujuannya juga sensitif, apa pun lokasinya
                elif stat.S_ISREG(info.st_mode):
                    inodes.add((info.st_dev, info.st_ino))
    return roots, inodes


def write_archive(
    target: BinaryIO,
    home: Path,
    paths: list[str],
    max_file_size: int = MAX_FILE_SIZE,
    exclude: list[str] = (),
    follow_symlinks: bool = False,
    max_total_size: int = MAX_TOTAL_SIZE,
) -> ArchiveReport:
    """Kemas path di home (relatif, tanpa '~/') ke tar.gz. Symlink dan file khusus tidak ikut (dicatat).

    `exclude` = path sensitif: tidak pernah masuk arsip ini, karena arsip ini tidak dienkripsi. Yang dibandingkan
    bukan hanya teks path, tapi juga lokasi asli (symlink seperti ~/.ssh -> ~/dotfiles/ssh) dan inode (hardlink),
    jadi isi sensitif tidak bisa ikut lewat folder dotfiles lain.

    `follow_symlinks` (arsip sensitif, yang dienkripsi): symlink, baik path yang diminta maupun di dalamnya, diikuti
    dan isi tujuannya dikemas dengan nama path symlink-nya, supaya isi sensitif tidak terlewat hanya karena berupa
    symlink. Folder yang sudah dikunjungi tidak diulang (aman dari symlink melingkar).
    """
    report = ArchiveReport()
    home = Path(home)
    exclude = list(exclude)
    excluded_roots, excluded_inodes = _identity(home, exclude)
    seen: set[str] = set()

    def is_excluded(rel: str, path: Path) -> bool:
        if any(rel == e or rel.startswith(e + "/") for e in exclude):
            return True
        if not excluded_roots:
            return False
        try:
            real = os.path.realpath(path)
            info = os.lstat(path)
        except OSError:
            return False
        if any(real == root or real.startswith(root.rstrip(os.sep) + os.sep) for root in excluded_roots):
            return True
        return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) in excluded_inodes

    def add_file(path: Path, rel: str, tar: tarfile.TarFile) -> None:
        if rel in seen:  # path yang tumpang-tindih di profil (mis. ~/.config dan ~/.config/nvim)
            return
        if is_excluded(rel, path):
            report.skipped.append((rel, "sensitif, hanya dibackup terenkripsi"))
            return
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) and follow_symlinks:
            target = Path(os.path.realpath(path))
            if not target.is_file():
                report.skipped.append((rel, "symlink ke path yang tidak ada / bukan file"))
                return
            report.followed.append((rel, str(target)))
            path, info = target, os.stat(target)
        if stat.S_ISLNK(info.st_mode):
            report.skipped.append((rel, "symlink"))
        elif not stat.S_ISREG(info.st_mode):
            report.skipped.append((rel, "bukan file biasa"))
        elif info.st_size > max_file_size:
            report.skipped.append((rel, f"lebih dari {max_file_size // (1024 * 1024)} MB"))
        elif report.total_size + info.st_size > max_total_size:
            report.skipped.append((rel, f"arsip sudah mencapai batas {max_total_size // (1024 * 1024)} MB"))
        else:
            tar.add(str(path), arcname=rel, recursive=False)
            seen.add(rel)
            report.files.append(rel)
            report.total_size += info.st_size

    def add_dir(path: Path, rel: str, tar: tarfile.TarFile) -> None:
        if rel not in seen:
            tar.add(str(path), arcname=rel, recursive=False)
            seen.add(rel)

    visited: set[str] = set()

    def add_tree(source: Path, rel: str, tar: tarfile.TarFile) -> None:
        """Isi folder `source` dengan nama arsip di bawah `rel`. Symlink folder hanya diikuti kalau follow_symlinks."""
        real = os.path.realpath(source)
        if real in visited:
            report.skipped.append((rel, "folder yang sama sudah dikemas (symlink melingkar?)"))
            return
        visited.add(real)
        add_dir(source, rel, tar)
        for root, dirs, names in os.walk(source):  # followlinks=False: symlink folder ditangani di bawah
            root_path = Path(root)
            keep = []
            for name in sorted(dirs):
                sub = root_path / name
                sub_rel = f"{rel}/{sub.relative_to(source).as_posix()}"
                if is_excluded(sub_rel, sub):
                    report.skipped.append((sub_rel, "sensitif, hanya dibackup terenkripsi"))
                elif sub.is_symlink():
                    if follow_symlinks and os.path.isdir(os.path.realpath(sub)):
                        report.followed.append((sub_rel, os.path.realpath(sub)))
                        add_tree(Path(os.path.realpath(sub)), sub_rel, tar)
                    else:
                        report.skipped.append((sub_rel, "symlink"))
                else:
                    add_dir(sub, sub_rel, tar)
                    keep.append(name)
            dirs[:] = keep
            for name in sorted(names):
                file_path = root_path / name
                add_file(file_path, f"{rel}/{file_path.relative_to(source).as_posix()}", tar)

    with tarfile.open(fileobj=target, mode="w:gz") as tar:
        for rel in paths:
            path = home / rel
            if not os.path.lexists(path):
                report.skipped.append((rel, "tidak ada"))
                continue
            source = path
            if follow_symlinks and path.is_symlink():
                resolved = Path(os.path.realpath(path))
                if not resolved.exists():
                    report.skipped.append((rel, "symlink ke path yang tidak ada"))
                    continue
                source = resolved
                report.followed.append((rel, str(resolved)))
            if source.is_symlink() or not source.is_dir():
                add_file(source, rel, tar)
                continue
            if is_excluded(rel, source):
                report.skipped.append((rel, "sensitif, hanya dibackup terenkripsi"))
                continue
            add_tree(source, rel, tar)
    return report
