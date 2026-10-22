"""Status BitLocker / Device Encryption lewat WMI Win32_EncryptableVolume.

Dipilih WMI (bukan teks `manage-bde -status`) karena nilainya berupa angka, tidak bergantung bahasa.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .runner import Runner, powershell_args

BITLOCKER_SCRIPT = r"""# distrorelay:bitlocker
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$volumes = @(Get-CimInstance -Namespace 'root\cimv2\Security\MicrosoftVolumeEncryption' -ClassName Win32_EncryptableVolume | ForEach-Object {
  [pscustomobject]@{
    DriveLetter = $_.DriveLetter; ProtectionStatus = $_.ProtectionStatus
    ConversionStatus = $_.ConversionStatus; EncryptionMethod = $_.EncryptionMethod; VolumeType = $_.VolumeType
  }
})
ConvertTo-Json -InputObject $volumes -Depth 3 -Compress
"""


class BitLockerError(RuntimeError):
    pass


@dataclass
class BitLockerVolume:
    drive_letter: str | None
    protection_status: int | None  # 0 = tidak terproteksi, 1 = terproteksi, 2 = tidak diketahui/terkunci
    conversion_status: int | None  # 0 = tidak terenkripsi, 1 = terenkripsi penuh, 2..5 = sedang proses
    encryption_method: int | None
    volume_type: int | None  # 0 = volume OS

    @property
    def encrypted(self) -> bool:
        return self.conversion_status not in (None, 0)

    @property
    def needs_recovery_key(self) -> bool:
        """Perlu recovery key kalau boot config berubah (dicek ulang di preflight M2)."""
        return self.encrypted and self.protection_status == 1

    def describe(self) -> str:
        if self.protection_status == 2:
            return "status tidak diketahui / volume terkunci"
        if self.conversion_status == 0:
            return "tidak terenkripsi"
        if self.conversion_status == 1:
            if self.protection_status == 1:
                return "terenkripsi, proteksi AKTIF (siapkan recovery key sebelum mengubah boot)"
            return "terenkripsi, proteksi tidak aktif/ditangguhkan"
        progress = {2: "sedang dienkripsi", 3: "sedang didekripsi", 4: "enkripsi dijeda", 5: "dekripsi dijeda"}
        return progress.get(self.conversion_status, f"status konversi {self.conversion_status}")


def _int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def parse_bitlocker(text: str) -> list[BitLockerVolume]:
    data = json.loads(text) if text.strip() else []
    if isinstance(data, dict):
        data = [data]
    return [
        BitLockerVolume(
            drive_letter=(v.get("DriveLetter") or None),
            protection_status=_int(v.get("ProtectionStatus")),
            conversion_status=_int(v.get("ConversionStatus")),
            encryption_method=_int(v.get("EncryptionMethod")),
            volume_type=_int(v.get("VolumeType")),
        )
        for v in data
    ]


def load_bitlocker(runner: Runner) -> tuple[list[BitLockerVolume], str]:
    result = runner.run(powershell_args(BITLOCKER_SCRIPT))
    if not result.ok:
        raise BitLockerError(f"Gagal membaca status BitLocker: {result.output() or result.returncode}")
    return parse_bitlocker(result.stdout), result.stdout
