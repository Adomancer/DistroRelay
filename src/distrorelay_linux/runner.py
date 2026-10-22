"""Satu pintu untuk perintah sistem di Linux kit, supaya test bisa memakai FakeRunner."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class Command:
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()  # variabel tambahan (dipakai kalau sudah root; tanpa root lewat `sudo VAR=..`)
    cwd: str | None = None

    def display(self) -> str:
        prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in self.env)
        where = f"(cd {shlex.quote(self.cwd)} && " if self.cwd else ""
        text = prefix + " ".join(shlex.quote(a) for a in self.args)
        return f"{where}{text})" if self.cwd else text


def command(*args: str, env: dict | None = None, cwd: str | None = None) -> Command:
    return Command(tuple(args), tuple(sorted((env or {}).items())), cwd)


@dataclass(frozen=True)
class Result:
    command: Command
    returncode: int
    stdout: str = ""
    stderr: str = ""
    data: bytes = b""  # stdout mentah untuk run_bytes (mis. hasil gpg --decrypt)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def output(self) -> str:
        return "\n".join(s for s in (self.stdout.strip(), self.stderr.strip()) if s)


class SubprocessRunner:
    def run_bytes(self, cmd: Command, data: bytes = b"") -> Result:
        """stdin dan stdout biner (gpg). stderr tetap ke terminal, jadi prompt passphrase gpg terlihat."""
        env = dict(os.environ, **dict(cmd.env)) if cmd.env else None
        try:
            proc = subprocess.run(list(cmd.args), input=data, stdout=subprocess.PIPE, env=env, cwd=cmd.cwd)
        except FileNotFoundError:
            return Result(cmd, 127, "", f"{cmd.args[0]}: perintah tidak ditemukan")
        return Result(cmd, proc.returncode, data=proc.stdout)

    def run(self, cmd: Command, *, capture: bool = True) -> Result:
        """capture=False: output (dan prompt sudo) langsung ke terminal, untuk perintah yang lama."""
        env = dict(os.environ, **dict(cmd.env)) if cmd.env else None
        try:
            if capture:
                proc = subprocess.run(
                    list(cmd.args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                    env=env, cwd=cmd.cwd,
                )
                return Result(cmd, proc.returncode, proc.stdout, proc.stderr)
            proc = subprocess.run(list(cmd.args), env=env, cwd=cmd.cwd)
            return Result(cmd, proc.returncode)
        except FileNotFoundError:
            return Result(cmd, 127, "", f"{cmd.args[0]}: perintah tidak ditemukan")

    def which(self, name: str) -> bool:
        return shutil.which(name) is not None


@dataclass
class FakeRunner:
    """Untuk test: respons per awalan perintah (teks `display()`), semua panggilan dicatat."""

    responses: dict[str, tuple[int, str]] = field(default_factory=dict)
    programs: set[str] = field(default_factory=set)
    calls: list[Command] = field(default_factory=list)
    # Untuk run_bytes: awalan perintah -> fungsi(command, input) -> (returncode, output bytes)
    byte_handlers: dict = field(default_factory=dict)

    def add(self, prefix: str, returncode: int = 0, stdout: str = "") -> None:
        self.responses[prefix] = (returncode, stdout)

    def run(self, cmd: Command, *, capture: bool = True) -> Result:
        self.calls.append(cmd)
        text = " ".join(cmd.args)
        match = max((p for p in self.responses if text.startswith(p)), key=len, default=None)
        returncode, stdout = self.responses[match] if match is not None else (0, "")
        return Result(cmd, returncode, stdout)

    def run_bytes(self, cmd: Command, data: bytes = b"") -> Result:
        self.calls.append(cmd)
        text = " ".join(cmd.args)
        match = max((p for p in self.byte_handlers if text.startswith(p)), key=len, default=None)
        if match is None:
            return Result(cmd, 127, "", f"FakeRunner: tidak ada handler untuk {text!r}")
        returncode, output = self.byte_handlers[match](cmd, data)
        return Result(cmd, returncode, data=output)

    def which(self, name: str) -> bool:
        return name in self.programs

    def ran(self) -> list[str]:
        return [" ".join(c.args) for c in self.calls]


def as_root(args: Sequence[str], is_root: bool, env: dict | None = None) -> Command:
    """Perintah yang butuh root: langsung kalau sudah root, kalau belum lewat sudo (variabel env ikut di argumen sudo)."""
    if is_root:
        return command(*args, env=env)
    return command("sudo", *[f"{k}={v}" for k, v in sorted((env or {}).items())], *args)
