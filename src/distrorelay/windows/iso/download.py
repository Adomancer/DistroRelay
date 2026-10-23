"""Unduhan HTTP(S) yang bisa dilanjutkan.

File ditulis ke `<nama>.part`. Kalau koneksi putus, unduhan dilanjutkan dengan header Range (otomatis beberapa
kali, dan juga saat perintahnya dijalankan lagi). Server yang tidak mendukung Range (status 200) membuat unduhan
diulang dari awal. Isi file tidak dipercaya di sini; verifikasinya di `verify.py`.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import distrorelay

from . import IsoError

log = logging.getLogger(__name__)

USER_AGENT = f"DistroRelay/{distrorelay.__version__}"
CHUNK = 1024 * 1024
# Feed, listing, file checksum, dan tanda tangan; jauh lebih kecil dari ini.
SMALL_LIMIT = 16 * 1024 * 1024

Progress = Callable[[int, int | None], None]  # (byte yang sudah ada, total kalau diketahui)


class DownloadError(IsoError):
    pass


class DownloadStopped(DownloadError):
    """Dihentikan pengguna; file .part disimpan untuk dilanjutkan."""


def _request(url: str, start: int = 0) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.Request(url, headers=headers)


def _check_scheme(url: str, require_https: bool) -> None:
    if require_https and not url.startswith("https://"):
        raise DownloadError(f"Hanya HTTPS yang dipakai: {url}")


_TRANSIENT_HTTP = (408, 429, 500, 502, 503, 504)


def fetch(
    url: str,
    *,
    limit: int = SMALL_LIMIT,
    timeout: float = 20,  # feed & checksum kecil; 60 dtk x 3 percobaan membuat `iso check` menggantung lama
    require_https: bool = True,
    retries: int = 2,
    backoff: float = 2.0,
) -> bytes:
    """Ambil file kecil (feed, listing, checksum, tanda tangan) ke memori. Error sementara dicoba lagi."""
    _check_scheme(url, require_https)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(_request(url), timeout=timeout) as response:
                data = response.read(limit + 1)
            break
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            permanent = isinstance(exc, urllib.error.HTTPError) and exc.code not in _TRANSIENT_HTTP
            if permanent or attempt >= retries:
                raise DownloadError(f"Gagal mengambil {url}: {exc}") from exc
            time.sleep(backoff * (attempt + 1))
    if len(data) > limit:
        raise DownloadError(f"{url} lebih besar dari {limit} byte; bukan file yang diharapkan")
    return data


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


def download(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    progress: Progress | None = None,
    should_stop: Callable[[], bool] | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: float = 60,
    require_https: bool = True,
    on_sha256: Callable[[str], None] | None = None,
) -> Path:
    """Unduh `url` ke `dest`, melanjutkan `dest.part` kalau ada. Mengembalikan `dest`.

    `on_sha256(hex)` menerima SHA256 isi file yang ditulis, dihitung sambil mengunduh, supaya ISO besar tidak perlu
    dibaca ulang hanya untuk di-hash.
    """
    _check_scheme(url, require_https)
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        try:
            digest = _download_once(url, part, expected_size, progress, should_stop, timeout)
            if digest is not None:
                part.replace(dest)
                if on_sha256 is not None:
                    on_sha256(digest)
                return dest
        except DownloadStopped:
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in _TRANSIENT_HTTP:
                raise DownloadError(f"Server menolak {url}: HTTP {exc.code}") from exc
            log.warning("Unduhan %s terputus (%s)", url, exc)
            if attempt >= retries:
                raise DownloadError(
                    f"Unduhan terputus ({exc}). Jalankan perintahnya lagi untuk melanjutkan dari {part.name}."
                ) from exc
            time.sleep(backoff * (attempt + 1))
        attempt += 1


def _sha256_of(path: Path) -> "hashlib._Hash":
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest


def _download_once(url, part: Path, expected_size, progress, should_stop, timeout) -> str | None:
    """SHA256 isi file kalau sudah lengkap, None kalau harus diulang dari awal. Error koneksi diteruskan ke
    pemanggil supaya dicoba lagi."""
    have = part.stat().st_size if part.exists() else 0
    if expected_size is not None and have > expected_size:
        part.unlink()
        have = 0
    if expected_size is not None and have == expected_size and have:
        return _sha256_of(part).hexdigest()
    try:
        response = urllib.request.urlopen(_request(url, have), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            # Range di luar ukuran file: .part sudah lengkap, atau file di server berganti. Ulang dari awal
            # kecuali ukurannya tepat seperti yang diharapkan.
            if expected_size is not None and have == expected_size:
                return _sha256_of(part).hexdigest()
            part.unlink()
            return None
        raise
    with response:
        status = response.status
        total = None
        if have and status == 206:
            match = _CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
            if not match or int(match.group(1)) != have:
                raise DownloadError(f"Server membalas Range yang tidak cocok untuk {url}")
            total = None if match.group(3) == "*" else int(match.group(3))
            mode = "ab"
        elif status == 200:
            have = 0  # server tidak mendukung Range, atau unduhan baru
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            mode = "wb"
        else:
            raise DownloadError(f"Status HTTP tak terduga {status} untuk {url}")
        if expected_size is not None and total is not None and total != expected_size:
            raise DownloadError(
                f"Ukuran file di server ({total} byte) tidak sama dengan feed ({expected_size} byte); dibatalkan"
            )
        total = total if total is not None else expected_size
        # Bagian yang dilanjutkan di-hash sekali dulu; sisanya di-hash sambil ditulis.
        digest = _sha256_of(part) if mode == "ab" else hashlib.sha256()
        with open(part, mode) as handle:
            while True:
                if should_stop is not None and should_stop():
                    raise DownloadStopped(f"Dihentikan; {part.name} disimpan untuk dilanjutkan")
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
                have += len(block)
                if progress is not None:
                    progress(have, total)
    if total is not None and have < total:
        raise ConnectionError(f"terputus di {have} dari {total} byte")
    if total is not None and have > total:
        part.unlink()
        raise DownloadError(f"Server mengirim lebih dari {total} byte; unduhan dibuang")
    return digest.hexdigest()
