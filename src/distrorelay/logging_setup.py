"""Logging ke file harian di %LOCALAPPDATA%\\DistroRelay\\logs."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .paths import logs_dir

# Log harian yang lebih lama dari ini dihapus saat DistroRelay dibuka. Journal eksekusi tidak pernah dihapus.
KEEP_DAYS = 30


def prune_logs(directory: Path, today: date, keep_days: int = KEEP_DAYS) -> list[Path]:
    """Hapus distrorelay-YYYY-MM-DD.log yang lebih tua dari `keep_days`. File lain tidak disentuh."""
    removed = []
    for path in directory.glob("distrorelay-*.log"):
        try:
            day = date.fromisoformat(path.stem.removeprefix("distrorelay-"))
        except ValueError:
            continue
        if (today - day).days > keep_days:
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path)
    return removed


def setup_logging(verbose: bool = False) -> Path | None:
    """Pasang file log. Mengembalikan path log, atau None kalau folder log tidak bisa dibuat."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"distrorelay-{date.today():%Y-%m-%d}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    prune_logs(directory, date.today())
    return path
