"""Logging ke file harian di %LOCALAPPDATA%\\DistroHop\\logs."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .paths import logs_dir


def setup_logging(verbose: bool = False) -> Path | None:
    """Pasang file log. Mengembalikan path log, atau None kalau folder log tidak bisa dibuat."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        directory = logs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"distrohop-{date.today():%Y-%m-%d}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    return path
