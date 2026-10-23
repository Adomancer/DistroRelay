"""SHA256 file per 1 MiB (file tidak pernah dimuat utuh ke memori). Satu implementasi untuk sisi Windows; Linux kit
punya `distrorelay_linux.kit.sha256_file` sendiri karena harus stdlib + Python 3.8 tanpa paket ini."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

CHUNK = 1024 * 1024
Progress = Callable[[int, int], None]  # (byte yang sudah dibaca, ukuran file)


def sha256_file(path: Path, progress: Progress | None = None) -> str:
    digest = hashlib.sha256()
    total = Path(path).stat().st_size if progress is not None else 0
    done = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
            if progress is not None:
                done += len(chunk)
                progress(done, total)
    return digest.hexdigest()
