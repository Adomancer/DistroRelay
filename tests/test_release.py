"""Rilis .exe (M7): isi spec PyInstaller, elevasi dari .exe, dan selftest."""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

from distrorelay import __version__, selftest
from distrorelay.windows import elevate, kit_export
from distrorelay.windows.iso import sources

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "distrorelay.spec"


def spec_data_files() -> set[Path]:
    """File yang dibawa spec sebagai data: tuple `(str(SRC / "a" / "*.py"), "tujuan")`."""
    files = set()
    for parts in re.findall(r'\(str\(SRC((?: / "[^"]+")+)\), "[^"]+"\)', SPEC.read_text(encoding="utf-8")):
        pattern = ROOT / "src" / Path(*re.findall(r'"([^"]+)"', parts))
        files |= {Path(p).resolve() for p in glob.glob(str(pattern))}
    return files


def test_spec_bundles_everything_the_exe_reads_from_disk():
    import distrorelay.common.packages as packages_mod
    from distrorelay_linux import backup as linux_backup

    needed = {path.resolve() for _, path in kit_export.kit_sources()}
    needed.add((Path(packages_mod.__file__).parent / "data" / "packages.toml").resolve())
    needed |= {p.resolve() for p in linux_backup._BASE_DIR.glob("*.txt")}
    needed |= {source.KEYS.path.resolve() for source in sources.SOURCES.values()}
    assert needed - spec_data_files() == set()


def test_versions_match():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject


def test_relaunch_command_from_python_and_from_exe(monkeypatch):
    program, params = elevate.relaunch_command(["--elevated-child", "scan", "--json", "C:\\a b\\x.json"])
    # K-1 (review 2026-10-23): -P = folder kerja tidak masuk sys.path proses Administrator.
    assert program == sys.executable and params.startswith("-P -m distrorelay --elevated-child scan")
    assert '"C:\\a b\\x.json"' in params
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\Tools\\distrorelay.exe")
    assert elevate.relaunch_command(["--elevated-child", "tui"]) == ("C:\\Tools\\distrorelay.exe", "--elevated-child tui")


def test_selftest_passes_from_source():
    checks = selftest.run()
    assert [c.name for c in checks if not c.ok] == [], [(c.name, c.message) for c in checks]
