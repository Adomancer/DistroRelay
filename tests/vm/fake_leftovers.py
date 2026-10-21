"""Buat sisa Linux palsu di VM Windows, meniru kondisi laptop asli (skenario VM tanpa install Linux).

HANYA untuk VM. Script menolak jalan kalau komputernya bukan VirtualBox/VMware, karena script ini
menimpa EFI\\Boot\\bootx64.efi, menambah entri boot, dan membuat partisi baru.

Yang dibuat (sama seperti hasil scan laptop asli 2026-10-21):
- Partisi #5: 2 GB, tipe XBOOTLDR, header ext4 palsu "terakhir di-mount di /boot"
- ESP: EFI\\fedora (shim + GRUB + stub grub.cfg ke partisi #5 + BOOTX64.CSV), EFI\\Arch (GRUB dengan
  prefix "(,gpt4)/grub"), grub\\ yatim di root ESP, mach_kernel + System\\, EFI\\HP (file pabrikan)
- Fallback EFI\\Boot\\bootx64.efi ditimpa shim palsu yang identik dengan EFI\\fedora\\shimx64.efi
- Entri boot "Fedora" dan "Arch" (lewat bcdedit /copy {bootmgr}) di urutan pertama, Windows sesudahnya
- Disk kedua yang masih kosong (RAW) di-format NTFS jadi drive E: untuk salinan backup

Semua file .efi palsu bukan program sungguhan: firmware VM akan gagal menjalankannya lalu lanjut ke
Windows Boot Manager. Tidak ada perintah undo: kembalikan snapshot VM.

Pemakaian (PowerShell Administrator, di dalam VM, dari folder repo):
    .venv\\Scripts\\python.exe tests\\vm\\fake_leftovers.py
    .venv\\Scripts\\python.exe tests\\vm\\fake_leftovers.py --no-partition   # tanpa partisi #5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import uuid
from pathlib import Path

from distrohop.windows import flush, gpt_types
from distrohop.windows.disks import MIB, DiskInventory, load_inventory
from distrohop.windows.elevate import is_admin
from distrohop.windows.esp import mounted_esp
from distrohop.windows.fsprobe import probe_bytes
from distrohop.windows.runner import SubprocessRunner, powershell_args

GIB = 1024 * MIB
BOOT_SIZE = 2 * GIB
HEAD_SIZE = 128 * 1024
BOOT_UUID = "d15f0b00-7e57-4c0f-9a11-000000000005"  # filesystem /boot Fedora palsu (partisi #5)
ARCH_ROOT_UUID = "a7c40000-7e57-4000-8000-000000000007"  # root Arch yang "sudah di-wipe"
RECORD_DIR = Path(r"C:\DistroHopVM")

FAKE_SHIM = b"MZ DistroHop VM: fake UEFI SHIM signed by Microsoft Corporation UEFI CA 2011 (bukan program)"
FEDORA_CSV = "\ufeffshimx64.efi,Fedora,,This is the boot entry for Fedora\n".encode("utf-16-le")
ENTRIES = [("Fedora", r"\EFI\fedora\shimx64.efi"), ("Arch", r"\EFI\Arch\grubx64.efi")]
FALLBACK = r"EFI\Boot\bootx64.efi"
_GUID = re.compile(r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}")


class SetupError(RuntimeError):
    pass


# --- Isi palsu (dipakai juga oleh tests/test_vm_fake_leftovers.py) ------------


def grub_core_image(prefix: str) -> bytes:
    """Core image GRUB dengan blok modul 'mimg' berisi prefix, seperti buatan grub-mkimage."""
    payload = prefix.encode() + b"\x00"
    payload += b"\x00" * (-len(payload) % 8)
    module = struct.pack("<II", 3, 8 + len(payload)) + payload
    info = struct.pack("<IIQQ", 0x676D696D, 0, 24, 24 + len(module))
    return b"MZ DistroHop VM: fake GNU GRUB core (bukan program)" + bytes(64) + info + module


def ext4_head(fs_uuid: str = BOOT_UUID, last_mounted: str = "/boot") -> bytes:
    """128 KiB awal partisi: superblock ext4 minimal yang dikenali fsprobe."""
    data = bytearray(HEAD_SIZE)
    sb = 1024
    struct.pack_into("<II", data, sb + 0x2C, 1_780_000_000, 1_780_000_060)
    struct.pack_into("<H", data, sb + 0x38, 0xEF53)
    struct.pack_into("<II", data, sb + 0x5C, 0x4, 0x40 | 0x200)
    data[sb + 0x68 : sb + 0x78] = uuid.UUID(fs_uuid).bytes
    data[sb + 0x88 : sb + 0x88 + len(last_mounted)] = last_mounted.encode()
    return bytes(data)


def esp_files(boot_uuid: str = BOOT_UUID) -> dict[str, bytes]:
    """File yang ditulis ke ESP (path relatif ESP). EFI\\Boot\\bootx64.efi ditimpa, sisanya file baru."""
    files: dict[str, bytes | str] = {
        FALLBACK: FAKE_SHIM,
        "EFI\\Boot\\BOOTIA32.EFI": b"MZ DistroHop VM: fake UEFI SHIM ia32",
        "EFI\\Boot\\fbx64.efi": b"MZ DistroHop VM: fake shim fallback",
        "EFI\\fedora\\shimx64.efi": FAKE_SHIM,
        "EFI\\fedora\\shimia32.efi": b"MZ DistroHop VM: fake UEFI SHIM ia32",
        "EFI\\fedora\\grubx64.efi": b"MZ DistroHop VM: fake GNU GRUB signed",
        "EFI\\fedora\\mmx64.efi": b"MZ DistroHop VM: fake MokManager",
        "EFI\\fedora\\BOOTX64.CSV": FEDORA_CSV,
        "EFI\\fedora\\grub.cfg": f"search --no-floppy --root-dev-only --fs-uuid --set=dev {boot_uuid}\n"
        "set prefix=($dev)/grub2\nexport $prefix\nconfigfile $prefix/grub.cfg\n",
        "EFI\\Arch\\grubx64.efi": grub_core_image("(,gpt4)/grub"),
        "grub\\grub.cfg": "menuentry 'Arch Linux' {\n"
        f"  search --no-floppy --fs-uuid --set=root {ARCH_ROOT_UUID}\n"
        f"  linux /vmlinuz-linux root=UUID={ARCH_ROOT_UUID} rw\n}}\n",
        "grub\\grubenv": "# GRUB Environment Block\n" + "#" * 999,
        "grub\\x86_64-efi\\normal.mod": b"DistroHop VM: fake GRUB module",
        "grub\\x86_64-efi\\core.efi": grub_core_image("(,gpt1)/grub"),
        "grub\\locale\\id.mo": b"DistroHop VM: fake locale",
        "EFI\\HP\\BIOS\\Current\\FAKE.bin": b"DistroHop VM: fake HP BIOS image",
        "EFI\\HP\\SystemDiags\\SysDiags.efi": b"MZ DistroHop VM: fake HP diagnostics",
        "mach_kernel": b"x" * 34,
        "System\\Library\\CoreServices\\SystemVersion.plist": "<plist/>",
    }
    return {path: content.encode() if isinstance(content, str) else content for path, content in files.items()}


# --- Langkah-langkah ----------------------------------------------------------


def run_ps(runner: SubprocessRunner, script: str) -> str:
    result = runner.run(powershell_args("$ErrorActionPreference = 'Stop'\n" + script), timeout=300)
    if not result.ok:
        raise SetupError(result.output() or f"PowerShell gagal (kode {result.returncode})")
    return result.stdout.strip()


def run_bcdedit(runner: SubprocessRunner, *args: str) -> str:
    result = runner.run(["bcdedit", *args])
    if not result.ok:
        raise SetupError(f"bcdedit {' '.join(args)} gagal: {result.output()}")
    return result.stdout


def check_is_vm(runner: SubprocessRunner) -> str:
    info = json.loads(run_ps(runner, "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model | ConvertTo-Json"))
    text = f"{info.get('Manufacturer') or ''} {info.get('Model') or ''}".strip()
    if not any(mark in text.lower() for mark in ("virtualbox", "innotek", "vmware")):
        raise SetupError(f"Ini bukan VM ({text}). Script ini hanya boleh dijalankan di VirtualBox/VMware.")
    return text


def boot_partition_slot(inventory: DiskInventory, disk_number: int) -> int:
    """Offset (rata 1 MiB) di ruang kosong terbesar disk Windows yang muat partisi 2 GB."""
    regions = [r for r in inventory.free_regions() if r.disk_number == disk_number]
    for region in sorted(regions, key=lambda r: r.size, reverse=True):
        offset = -(-region.offset // MIB) * MIB
        if region.offset + region.size - offset >= BOOT_SIZE + MIB:
            return offset
    raise SetupError("Tidak ada ruang kosong 2 GB di disk Windows. Perbesar disk VM dulu, atau pakai --no-partition.")


def create_boot_partition(runner: SubprocessRunner, disk_number: int, offset: int) -> dict:
    out = run_ps(
        runner,
        f"New-Partition -DiskNumber {disk_number} -Offset {offset} -Size {BOOT_SIZE} "
        f"-GptType '{{{gpt_types.XBOOTLDR}}}' | Select-Object PartitionNumber, Offset, Size, Guid | ConvertTo-Json",
    )
    part = json.loads(out)
    if int(part["Offset"]) != offset:
        raise SetupError(f"Partisi dibuat di offset {part['Offset']}, bukan {offset}; header ext4 tidak ditulis.")
    # Partisi tanpa volume Windows: sektor di luar volume yang ter-mount boleh ditulis langsung.
    head = ext4_head()
    fd = os.open(rf"\\.\PhysicalDrive{disk_number}", os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        if os.write(fd, head) != len(head):
            raise SetupError("Header ext4 tidak tertulis penuh")
        os.lseek(fd, offset, os.SEEK_SET)
        info = probe_bytes(os.read(fd, len(head)))
    finally:
        os.close(fd)
    if info is None or info.fs_type != "ext4" or (info.uuid or "").lower() != BOOT_UUID or info.last_mounted != "/boot":
        raise SetupError(f"Header ext4 dibaca ulang tidak cocok: {info}")
    return part


def prepare_backup_disk(runner: SubprocessRunner, inventory: DiskInventory, windows_disk: int) -> str | None:
    """Format disk kosong (RAW, ≤ 64 GB, bukan disk Windows) jadi NTFS untuk salinan backup."""
    candidates = [
        d for d in inventory.disks
        if d.number != windows_disk and d.partition_style.upper() == "RAW" and d.size <= 64 * GIB
    ]
    if len(candidates) != 1:
        return None
    disk = candidates[0]
    letter = "-DriveLetter E" if not os.path.exists("E:\\") else "-AssignDriveLetter"
    out = run_ps(
        runner,
        f"Initialize-Disk -Number {disk.number} -PartitionStyle GPT\n"
        f"$p = New-Partition -DiskNumber {disk.number} -UseMaximumSize {letter}\n"
        "$null = Format-Volume -Partition $p -FileSystem NTFS -NewFileSystemLabel 'DH-BACKUP' -Confirm:$false\n"
        "(Get-Partition -DiskNumber $p.DiskNumber -PartitionNumber $p.PartitionNumber).DriveLetter",
    )
    return out.strip() or None


def write_esp(runner: SubprocessRunner, inventory: DiskInventory) -> None:
    files = esp_files()
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    with mounted_esp(runner, inventory.esp()) as mounted:
        root = mounted.root
        if not (root / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi").exists():
            raise SetupError("EFI\\Microsoft\\Boot\\bootmgfw.efi tidak ada di ESP; berhenti.")
        existing = [rel for rel in files if rel != FALLBACK and (root / rel).exists()]
        if existing:
            raise SetupError(f"Sudah ada di ESP: {', '.join(existing)}. Kembalikan snapshot VM dulu.")
        fallback = root / FALLBACK
        if fallback.exists():
            (RECORD_DIR / "bootx64.efi.asli").write_bytes(fallback.read_bytes())
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        flush.flush_volume(root)


def create_entries(runner: SubprocessRunner) -> dict[str, str]:
    created: dict[str, str] = {}
    for description, path in ENTRIES:
        match = _GUID.search(run_bcdedit(runner, "/copy", "{bootmgr}", "/d", description))
        if not match:
            raise SetupError(f"GUID entri '{description}' tidak terbaca dari output bcdedit /copy")
        created[description] = match.group(0)
        run_bcdedit(runner, "/set", created[description], "path", path)
    # /addfirst satu per satu dari belakang: hasilnya Fedora, Arch, lalu Windows Boot Manager.
    for description, _ in reversed(ENTRIES):
        run_bcdedit(runner, "/set", "{fwbootmgr}", "displayorder", created[description], "/addfirst")
    # Tanpa ini, Windows sempat membuat ulang entri dobel saat boot berikutnya (uji VM 2026-10-22).
    flush.flush_bcd()
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Buat sisa Linux palsu di VM Windows (khusus VM).")
    parser.add_argument("--no-partition", action="store_true", help="jangan buat partisi #5 (XBOOTLDR)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8")

    runner = SubprocessRunner()
    try:
        if not is_admin():
            raise SetupError("Jalankan dari PowerShell Administrator.")
        machine = check_is_vm(runner)
        inventory, _ = load_inventory(runner)
        esp = inventory.esp()
        if esp is None:
            raise SetupError("ESP tidak ditemukan.")
        if any(p.is_linux for p in inventory.partitions):
            raise SetupError("Sudah ada partisi Linux. Kembalikan snapshot VM dulu.")
        slot = None if args.no_partition else boot_partition_slot(inventory, esp.disk_number)

        print(f"Komputer: {machine}")
        print("Yang akan dibuat:")
        if slot is not None:
            print(f"  - partisi 2 GB tipe XBOOTLDR di disk {esp.disk_number}, offset {slot:,}, header ext4 '/boot'")
        print("  - EFI\\fedora, EFI\\Arch, grub\\, EFI\\HP, mach_kernel, System\\ di ESP")
        print("  - EFI\\Boot\\bootx64.efi ditimpa shim palsu (aslinya disimpan di C:\\DistroHopVM)")
        print("  - entri boot 'Fedora' dan 'Arch' di urutan pertama")
        print("  - disk kosong kedua (kalau ada) di-format NTFS untuk backup")
        if input("Ketik BUAT untuk lanjut: ").strip() != "BUAT":
            print("Dibatalkan.")
            return 1

        record: dict = {"machine": machine}
        if slot is not None:
            record["boot_partition"] = create_boot_partition(runner, esp.disk_number, slot)
            print(f"[ok] Partisi #{record['boot_partition']['PartitionNumber']} dibuat, header ext4 terverifikasi")
        record["backup_drive"] = prepare_backup_disk(runner, inventory, esp.disk_number)
        if record["backup_drive"]:
            print(f"[ok] Drive backup siap: {record['backup_drive']}:")
        else:
            print("[--] Disk backup kosong tidak ditemukan (atau sudah di-format); dilewati")
        write_esp(runner, inventory)
        print("[ok] Sisa palsu ditulis ke ESP")
        record["entries"] = create_entries(runner)
        print(f"[ok] Entri boot dibuat: {record['entries']}")
        (RECORD_DIR / "fake-leftovers.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    except SetupError as exc:
        print(f"GAGAL: {exc}", file=sys.stderr)
        print("Kalau sebagian sudah terbuat, kembalikan snapshot VM sebelum mencoba lagi.", file=sys.stderr)
        return 2

    print("\nSelesai. Ambil snapshot VM sekarang (mis. 'sisa-palsu'), lalu jalankan:")
    print("  .venv\\Scripts\\python.exe -m distrohop scan")
    print("  .venv\\Scripts\\python.exe -m distrohop plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
