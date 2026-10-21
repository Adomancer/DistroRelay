"""Cek tanda tangan Authenticode file EFI lewat Get-AuthenticodeSignature (read-only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .runner import Runner, powershell_args

SIGNATURE_SCRIPT = r"""# distrohop:authenticode
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$s = Get-AuthenticodeSignature -LiteralPath '__PATH__'
$c = $s.SignerCertificate
[pscustomobject]@{
  Status = [string]$s.Status; Message = $s.StatusMessage
  Subject = $(if ($c) { $c.Subject } else { $null })
  Issuer = $(if ($c) { $c.Issuer } else { $null })
  Thumbprint = $(if ($c) { $c.Thumbprint } else { $null })
} | ConvertTo-Json -Compress
"""


class SignatureError(RuntimeError):
    pass


@dataclass
class Signature:
    status: str  # "Valid", "NotSigned", "HashMismatch", "UnknownError", ...
    message: str | None
    subject: str | None
    issuer: str | None
    thumbprint: str | None

    @property
    def is_microsoft(self) -> bool:
        return self.status == "Valid" and "O=Microsoft Corporation" in (self.subject or "")

    def describe(self) -> str:
        if self.is_microsoft:
            return f"valid, ditandatangani {self.subject}"
        return f"{self.status}: {self.message or '-'} (penanda tangan: {self.subject or 'tidak ada'})"


def signature_script(path: Path | str) -> str:
    return SIGNATURE_SCRIPT.replace("__PATH__", str(path).replace("'", "''"))


def parse_signature(text: str) -> Signature:
    data = json.loads(text)
    return Signature(
        status=data.get("Status") or "UnknownError",
        message=data.get("Message"),
        subject=data.get("Subject"),
        issuer=data.get("Issuer"),
        thumbprint=data.get("Thumbprint"),
    )


def check_signature(runner: Runner, path: Path | str) -> Signature:
    result = runner.run(powershell_args(signature_script(path)))
    if not result.ok:
        raise SignatureError(f"Gagal membaca tanda tangan {path}: {result.output() or result.returncode}")
    return parse_signature(result.stdout)
