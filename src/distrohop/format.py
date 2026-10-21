"""Helper format tampilan."""

from __future__ import annotations


def human_size(size: int | None) -> str:
    """Ukuran gaya Windows (basis 1024): 2147483648 -> '2.0 GB'."""
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
