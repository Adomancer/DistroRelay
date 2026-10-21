"""Lokasi folder data aplikasi (%LOCALAPPDATA%\\DistroHop)."""

from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "DistroHop"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def reports_dir() -> Path:
    return app_data_dir() / "reports"
