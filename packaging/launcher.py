"""Titik masuk .exe (PyInstaller): `__main__.py` memakai import relatif, jadi tidak bisa dijadikan script langsung."""

from distrorelay.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
