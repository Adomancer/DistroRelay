"""Verifikasi 2 lapis: tanda tangan GPG (gpgv + keyring bawaan) dan SHA256 ISO.

`gpgv` dijalankan dengan keyring bawaan yang disalin ke folder sementara, dan folder itu jadi direktori kerja.
Path keyring sengaja relatif: gpgv bawaan Git for Windows (MSYS) membaca `C:\\...` sebagai URL dan menolaknya,
sedangkan file data dengan path Windows tetap terbaca.

Lolos hanya kalau gpgv sukses, tidak ada satu pun tanda tangan yang buruk/kedaluwarsa/tidak dikenal, dan kunci
utama setiap tanda tangan ada di daftar fingerprint yang di-pin di kode (keyring yang dimodifikasi pun tidak
cukup untuk lolos).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...hashing import sha256_file
from ..runner import CommandResult
from . import CLEARSIGNED_CHECKSUM, DETACHED_ISO, KeySpec, Release

GPGV_ENV = "DISTRORELAY_GPGV"
INSTALL_HINT = "winget install GnuPG.GnuPG"
# Lokasi umum di Windows: GnuPG for Windows / Gpg4win, lalu gpgv yang ikut Git for Windows.
_GPGV_CANDIDATES = (r"GnuPG\bin\gpgv.exe", r"Git\usr\bin\gpgv.exe")
_BAD_STATUS = {"BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG", "NO_PUBKEY", "FAILURE"}
# Algoritma hash OpenPGP yang dianggap lemah: MD5, SHA1, RIPEMD160.
_WEAK_HASHES = {"1", "2", "3"}
# Algoritma kunci yang tidak diterima: Elgamal (16, 20) dan DSA (17, kunci rilis lama umumnya 1024 bit).
_WEAK_KEY_ALGOS = {"16", "17", "20"}

GpgvRun = Callable[[list[str], Path], CommandResult]


def find_gpgv() -> str | None:
    override = os.environ.get(GPGV_ENV)
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which("gpgv")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        for rel in _GPGV_CANDIDATES:
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return None


def run_gpgv(args: list[str], cwd: Path) -> CommandResult:
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    proc = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, cwd=cwd, timeout=1800, **kwargs)
    return CommandResult(
        tuple(args), proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    )


@dataclass
class SignatureCheck:
    ok: bool
    message: str
    signer: str | None = None  # fingerprint kunci utama
    signer_name: str | None = None
    signed_text: bytes | None = None  # isi yang ditandatangani (hanya untuk clearsigned)
    judged: bool = False  # gpgv sempat menilai tanda tangannya (bukan gagal karena alat/keyring)
    bad_signature: bool = False  # BADSIG: isi file tidak cocok dengan tanda tangannya (file rusak/diubah)


def parse_status(text: str) -> list[tuple[str, list[str]]]:
    lines = []
    for line in text.splitlines():
        if line.startswith("[GNUPG:] "):
            parts = line[9:].split()
            if parts:
                lines.append((parts[0], parts[1:]))
    return lines


def check_signature(
    keys: KeySpec,
    data: Path,
    signature: Path | None = None,
    *,
    gpgv: str,
    run: GpgvRun = run_gpgv,
) -> SignatureCheck:
    """Cek `signature` (terpisah) atas `data`, atau `data` yang clearsigned kalau `signature` None."""
    if not keys.path.is_file():
        return SignatureCheck(False, f"keyring bawaan {keys.path.name} tidak ada")
    with tempfile.TemporaryDirectory(prefix="distrorelay-gpgv-") as home:
        work = Path(home)
        shutil.copyfile(keys.path, work / "keyring.gpg")
        args = [gpgv, "--homedir", ".", "--keyring", "./keyring.gpg", "--status-fd", "1"]
        if signature is None:
            args += ["--output", "./signed.txt", str(Path(data).resolve())]
        else:
            args += [str(Path(signature).resolve()), str(Path(data).resolve())]
        try:
            result = run(args, work)
        except (OSError, subprocess.SubprocessError) as exc:
            return SignatureCheck(False, f"gpgv tidak bisa dijalankan: {exc}")
        verdict = _judge(keys, result)
        if verdict.ok and signature is None:
            try:
                verdict.signed_text = (work / "signed.txt").read_bytes()
            except OSError:
                return SignatureCheck(False, "gpgv tidak menghasilkan isi yang ditandatangani")
        return verdict


def _judge(keys: KeySpec, result: CommandResult) -> SignatureCheck:
    status = parse_status(result.stdout)
    bad = [keyword for keyword, _ in status if keyword in _BAD_STATUS]
    valid = [fields for keyword, fields in status if keyword == "VALIDSIG"]
    if bad:
        if "NO_PUBKEY" in bad or "ERRSIG" in bad:
            return SignatureCheck(False, "ditandatangani kunci yang tidak ada di keyring bawaan (bukan kunci resmi)",
                                  judged=True)
        return SignatureCheck(False, f"tanda tangan TIDAK sah ({', '.join(sorted(set(bad)))})", judged=True,
                              bad_signature="BADSIG" in bad)
    if not result.ok or not valid:
        detail = result.output().splitlines()[-1:] or [f"kode {result.returncode}"]
        # Tanpa status sama sekali = gpgv tidak sempat membaca tanda tangan (file bukan OpenPGP, alat rusak).
        return SignatureCheck(False, f"gpgv gagal: {detail[0]}", judged=bool(status))
    signers = []
    for fields in valid:
        primary = (fields[9] if len(fields) >= 10 else fields[0]).upper()
        if primary not in keys.fingerprints:
            return SignatureCheck(False, f"ditandatangani kunci {primary} yang tidak di-pin untuk distro ini",
                                  judged=True)
        if len(fields) > 7 and fields[7] in _WEAK_HASHES:
            return SignatureCheck(False, f"tanda tangan memakai algoritma hash lemah (id {fields[7]})", judged=True)
        if len(fields) > 6 and fields[6] in _WEAK_KEY_ALGOS:
            return SignatureCheck(False, f"tanda tangan memakai algoritma kunci lemah (id {fields[6]})", judged=True)
        signers.append(primary)
    name = next((" ".join(fields[1:]) for keyword, fields in status if keyword == "GOODSIG"), None)
    return SignatureCheck(True, f"sah, oleh {name or signers[0]}", signers[0], name, judged=True)


# --- Checksum ---------------------------------------------------------------------------

_GNU_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")
_BSD_LINE = re.compile(r"^SHA256 \((.+)\) = ([0-9a-fA-F]{64})$")


def parse_checksums(text: str) -> dict[str, str]:
    """SHA256 per nama file dari format GNU (`hash *nama`) atau BSD (`SHA256 (nama) = hash`).

    Nama yang muncul dua kali dengan hash berbeda dibuang (tidak bisa dipastikan mana yang benar).
    """
    found: dict[str, str] = {}
    conflicts = set()
    for raw in text.splitlines():
        line = raw.strip()
        gnu, bsd = _GNU_LINE.match(line), _BSD_LINE.match(line)
        if gnu:
            name, digest = gnu.group(2), gnu.group(1)
        elif bsd:
            name, digest = bsd.group(1), bsd.group(2)
        else:
            continue
        digest = digest.lower()
        if found.get(name, digest) != digest:
            conflicts.add(name)
        found[name] = digest
    for name in conflicts:
        found.pop(name, None)
    return found


# --- Verifikasi rilis -----------------------------------------------------------------------


@dataclass
class Step:
    name: str
    ok: bool
    message: str


@dataclass
class Verification:
    steps: list[Step]
    sha256: str | None = None  # SHA256 resmi (dari checksum bertanda tangan, atau feed untuk Arch)
    signer: str | None = None
    iso_bad: bool = False  # isi ISO terbukti rusak/diubah (SHA256/ukuran salah, atau BADSIG atas ISO Arch)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)


def signed_sha256(
    release: Release, folder: Path, keys: KeySpec, *, gpgv: str, run: GpgvRun = run_gpgv
) -> Verification:
    """Lapis 1: tanda tangan, lalu SHA256 resmi ISO yang diambil dari isi yang sudah terverifikasi.

    Arch menandatangani ISO-nya langsung, jadi lapis ini butuh ISO yang sudah terunduh.
    """
    folder = Path(folder)
    if release.scheme == DETACHED_ISO:
        signature, data = folder / release.signature_name, folder / release.iso_name
    elif release.scheme == CLEARSIGNED_CHECKSUM:
        signature, data = None, folder / release.checksum_name
    else:
        signature, data = folder / release.signature_name, folder / release.checksum_name
    missing = [p.name for p in (data, signature) if p is not None and not p.is_file()]
    if missing:
        return Verification([Step("Tanda tangan GPG", False, f"file tidak ada: {', '.join(missing)}")])
    check = check_signature(keys, data, signature, gpgv=gpgv, run=run)
    steps = [Step("Tanda tangan GPG", check.ok, f"{data.name}: {check.message}")]
    if not check.ok:
        # Kunci tidak dikenal/kedaluwarsa = masalah keyring, bukan bukti ISO-nya rusak: jangan disisihkan, karena
        # unduh ulang tidak akan menolong. Hanya BADSIG (isi tidak cocok dengan tanda tangannya) yang membuktikannya.
        return Verification(steps, iso_bad=release.scheme == DETACHED_ISO and check.bad_signature)
    if release.scheme == DETACHED_ISO:
        return Verification(steps, (release.sha256 or "").lower() or None, check.signer)
    text = check.signed_text if check.signed_text is not None else data.read_bytes()
    expected = parse_checksums(text.decode("utf-8", "replace")).get(release.iso_name)
    if expected is None:
        steps.append(Step("Checksum resmi", False, f"{release.iso_name} tidak tercantum di {data.name}"))
        return Verification(steps)
    # Nilai dari feed (tidak bertanda tangan) diabaikan di sini; yang berlaku isi file bertanda tangan.
    return Verification(steps, expected, check.signer)


def verify_release(
    release: Release,
    folder: Path,
    keys: KeySpec,
    *,
    gpgv: str,
    run: GpgvRun = run_gpgv,
    progress: Callable[[int, int], None] | None = None,
    known_sha256: str | None = None,
) -> Verification:
    """Dua lapis lengkap atas ISO yang sudah terunduh. `known_sha256` = SHA256 yang dihitung sambil mengunduh file
    itu barusan (tidak dibaca ulang)."""
    result = signed_sha256(release, folder, keys, gpgv=gpgv, run=run)
    if not result.ok:
        return result
    iso = Path(folder) / release.iso_name
    if not iso.is_file():
        result.steps.append(Step("SHA256 ISO", False, f"{iso.name} belum terunduh"))
        return result
    if release.size is not None and iso.stat().st_size != release.size:
        result.steps.append(Step("Ukuran ISO", False, f"{iso.stat().st_size} byte, feed bilang {release.size}"))
        result.iso_bad = True
        return result
    if not result.sha256:
        result.steps.append(Step("SHA256 ISO", False, "tidak ada nilai SHA256 resmi untuk dibandingkan"))
        return result
    actual = known_sha256.lower() if known_sha256 else sha256_file(iso, progress)
    match = actual == result.sha256
    result.steps.append(Step("SHA256 ISO", match, actual if match else f"{actual} ≠ resmi {result.sha256}"))
    result.iso_bad = not match
    return result
