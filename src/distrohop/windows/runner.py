"""Satu pintu untuk semua perintah sistem.

Semua modul menjalankan perintah lewat `Runner`, sehingga:
- test bisa memakai `FakeRunner` tanpa menyentuh sistem;
- scan dibungkus `ReadOnlyRunner`, yang menolak perintah apa pun yang bisa mengubah sistem.
"""

from __future__ import annotations

import base64
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def output(self) -> str:
        """stdout + stderr dalam satu teks, untuk pesan error."""
        return "\n".join(s for s in (self.stdout.strip(), self.stderr.strip()) if s)


class Runner(Protocol):
    def run(self, args: Sequence[str], *, timeout: float = 120) -> CommandResult: ...


class ReadOnlyViolation(PermissionError):
    """Perintah ditolak karena bisa mengubah sistem."""


# Setiap script PowerShell diawali penanda `# distrohop:<nama>` supaya bisa dikenali di test & log.
_SCRIPT_MARKER = re.compile(r"^#\s*distrohop:(?P<name>[\w-]+)", re.MULTILINE)


def powershell_args(script: str) -> list[str]:
    """Bungkus script jadi argumen powershell.exe (lewat -EncodedCommand supaya bebas masalah quoting)."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


def decode_powershell(args: Sequence[str]) -> str | None:
    """Isi script kalau `args` dibuat oleh `powershell_args`, selain itu None."""
    if not args or _exe_name(args[0]) != "powershell":
        return None
    lowered = [a.lower() for a in args]
    try:
        encoded = args[lowered.index("-encodedcommand") + 1]
        return base64.b64decode(encoded).decode("utf-16-le")
    except (ValueError, IndexError):
        return None


def command_key(args: Sequence[str]) -> str:
    """Nama pendek perintah: `ps:<nama script>` untuk PowerShell, selain itu perintah apa adanya."""
    script = decode_powershell(args)
    if script is not None:
        match = _SCRIPT_MARKER.search(script)
        return f"ps:{match.group('name')}" if match else "ps:?"
    return " ".join([_exe_name(args[0]), *args[1:]])


def _exe_name(arg0: str) -> str:
    name = re.split(r"[\\/]", arg0)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def console_encoding() -> str:
    """Encoding output program konsol Windows yang di-redirect (OEM code page)."""
    if sys.platform == "win32":
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    return "utf-8"


class SubprocessRunner:
    """Runner sungguhan."""

    def run(self, args: Sequence[str], *, timeout: float = 120) -> CommandResult:
        kwargs = {}
        if sys.platform == "win32":
            # Jangan tempel ke konsol TUI dan jangan munculkan jendela baru.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            list(args), capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, **kwargs
        )
        # Script kita memaksa [Console]::OutputEncoding = UTF8; program lain memakai OEM code page.
        encoding = "utf-8" if decode_powershell(args) is not None else console_encoding()
        return CommandResult(
            tuple(args),
            proc.returncode,
            proc.stdout.decode(encoding, "replace"),
            proc.stderr.decode(encoding, "replace"),
        )


@dataclass
class FakeRunner:
    """Runner untuk test: respons ditentukan per `command_key`, dan semua panggilan dicatat."""

    responses: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def add(self, key: str, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.responses[key] = (returncode, stdout, stderr)

    def run(self, args: Sequence[str], *, timeout: float = 120) -> CommandResult:
        self.calls.append(tuple(args))
        key = command_key(args)
        if key not in self.responses:
            return CommandResult(tuple(args), 127, "", f"FakeRunner: tidak ada respons untuk {key!r}")
        returncode, stdout, stderr = self.responses[key]
        return CommandResult(tuple(args), returncode, stdout, stderr)


# --- Penjaga read-only -------------------------------------------------------

ALLOWED_CMDLETS = frozenset(
    {
        "get-disk",
        "get-partition",
        "get-volume",
        "get-ciminstance",
        "convertto-json",
        "foreach-object",
        "select-object",
        "where-object",
        "sort-object",
    }
)
_CMDLET = re.compile(r"\b[a-z]+-[a-z]+\b", re.IGNORECASE)
_BCDEDIT_ENUM_ARG = re.compile(r"^(/v|firmware|all|active|\{[0-9a-z-]+\})$", re.IGNORECASE)
_DRIVE = re.compile(r"^[a-z]:\\?$", re.IGNORECASE)


def check_read_only(args: Sequence[str]) -> None:
    """Lempar `ReadOnlyViolation` kalau perintah bisa mengubah sistem.

    Satu-satunya pengecualian adalah `mountvol X: /S` dan `mountvol X: /D`, yang me-mount ESP
    sementara selama scan. Ini menambah/menghapus drive letter, tapi tidak mengubah isi disk.
    """
    if not args:
        raise ReadOnlyViolation("Perintah kosong")
    script = decode_powershell(args)
    if script is not None:
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        used = {m.group(0).lower() for m in _CMDLET.finditer(code)}
        forbidden = sorted(used - ALLOWED_CMDLETS)
        if forbidden:
            raise ReadOnlyViolation(f"Script PowerShell memakai cmdlet di luar daftar read-only: {forbidden}")
        return
    exe = _exe_name(args[0])
    rest = [a.lower() for a in args[1:]]
    if exe == "bcdedit" and rest[:1] == ["/enum"] and all(_BCDEDIT_ENUM_ARG.match(a) for a in rest[1:]):
        return
    if exe == "manage-bde" and rest[:1] == ["-status"]:
        return
    if exe == "mountvol" and len(rest) == 2 and _DRIVE.match(rest[0]) and rest[1] in ("/s", "/d"):
        return
    raise ReadOnlyViolation(f"Perintah tidak diizinkan dalam mode read-only: {command_key(args)}")


@dataclass
class ReadOnlyRunner:
    inner: Runner

    def run(self, args: Sequence[str], *, timeout: float = 120) -> CommandResult:
        check_read_only(args)
        return self.inner.run(args, timeout=timeout)
