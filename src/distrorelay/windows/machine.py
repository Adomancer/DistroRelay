"""Kenali apakah DistroRelay jalan di mesin virtual atau di komputer fisik (read-only, WMI).

Dipakai supaya "ini VM" bukan sekadar pengakuan: di mesin fisik, mode VM tidak bisa dipilih.
Deteksi yang gagal dianggap mesin fisik.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .runner import Runner, powershell_args

MACHINE_SCRIPT = """# distrorelay:machine
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model | ConvertTo-Json -Compress
"""

# Penanda di Manufacturer/Model. Hyper-V: "Microsoft Corporation" + "Virtual Machine".
_VM_MARKERS = ("virtualbox", "innotek", "vmware", "qemu", "kvm", "virtual machine", "parallels", "xen", "bochs")


@dataclass(frozen=True)
class Machine:
    manufacturer: str
    model: str
    is_vm: bool

    @property
    def description(self) -> str:
        return f"{self.manufacturer} {self.model}".strip() or "tidak dikenal"


UNKNOWN = Machine("", "", False)


def parse_machine(text: str) -> Machine:
    try:
        info = json.loads(text)
    except ValueError:
        return UNKNOWN
    if not isinstance(info, dict):
        return UNKNOWN
    manufacturer = str(info.get("Manufacturer") or "").strip()
    model = str(info.get("Model") or "").strip()
    combined = f"{manufacturer} {model}".lower()
    return Machine(manufacturer, model, any(marker in combined for marker in _VM_MARKERS))


def detect_machine(runner: Runner) -> Machine:
    result = runner.run(powershell_args(MACHINE_SCRIPT))
    return parse_machine(result.stdout) if result.ok else UNKNOWN
