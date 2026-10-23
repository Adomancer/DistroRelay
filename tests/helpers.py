"""Pembuat data sintetis untuk test: entri NVRAM, superblock, PE/UKI, dan ESP palsu."""

from __future__ import annotations

import json
import re
import struct
import uuid
from contextlib import contextmanager
from pathlib import Path

from distrorelay.windows import disks
from distrorelay.windows.esp import MountedEsp, scan_esp
from distrorelay.windows.nvram import snapshot_from_raw
from distrorelay.windows.runner import FakeRunner, ReadOnlyRunner

FIXTURES = Path(__file__).parent / "fixtures"

# GUID dari fixture laptop asli (tests/fixtures/disks_laptop.json)
ESP_GUID = "55aefd10-5fa7-4724-b83b-0fe2e6911ae0"
P5_GUID = "ec0d38dc-a5af-4ee5-8096-66da7272fcac"
BOOT_UUID = "3f1c2b9a-6d1e-4f3a-9a7b-2c5d8e1f0a4b"
WINDOWS_BCD = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def laptop_inventory() -> disks.DiskInventory:
    return disks.parse_inventory(fixture_text("disks_laptop.json"))


def add_partition(inventory: disks.DiskInventory, number: int, gpt_type: str, guid: str, size: int = 50 * 2**30):
    """Tambahkan partisi sintetis setelah partisi terakhir di disk 0."""
    from distrorelay.windows import gpt_types

    info = gpt_types.lookup(gpt_type)
    last = inventory.partitions_on(0)[-1]
    part = disks.Partition(
        disk_number=0,
        number=number,
        guid=gpt_types.normalize_guid(guid),
        gpt_type=gpt_type,
        type_name=info.name,
        category=info.category,
        offset=last.end + 2**20,
        size=size,
        drive_letter=None,
        access_paths=[],
        is_boot=False,
        is_system=False,
        is_hidden=True,
        windows_type="Unknown",
        filesystem=None,
        label=None,
    )
    inventory.partitions.append(part)
    return part


# --- EFI_LOAD_OPTION ---------------------------------------------------------

END_NODE = struct.pack("<BBH", 0x7F, 0xFF, 4)


def hd_node(part_no: int, guid: str, start: int = 2048, size: int = 532480) -> bytes:
    data = struct.pack("<IQQ", part_no, start, size) + uuid.UUID(guid).bytes_le + bytes([2, 2])
    return struct.pack("<BBH", 0x04, 0x01, 4 + len(data)) + data


def file_node(path: str) -> bytes:
    data = (path + "\x00").encode("utf-16-le")
    return struct.pack("<BBH", 0x04, 0x04, 4 + len(data)) + data


def raw_node(node_type: int, subtype: int, length: int) -> bytes:
    return struct.pack("<BBH", node_type, subtype, 4 + length) + bytes(length)


def load_option(description: str, nodes: list[bytes], optional: bytes = b"", attributes: int = 1) -> bytes:
    device_path = b"".join(nodes) + END_NODE
    return (
        struct.pack("<IH", attributes, len(device_path))
        + (description + "\x00").encode("utf-16-le")
        + device_path
        + optional
    )


def windows_optional_data(bcd_id: str = WINDOWS_BCD) -> bytes:
    return b"WINDOWS\x00" + struct.pack("<II", 1, 0x88) + f"BCDOBJECT={bcd_id}\x00".encode("utf-16-le")


def nvram_raw_laptop() -> dict[str, str]:
    """NVRAM seperti dugaan kondisi laptop: Fedora di urutan pertama, lalu Windows, lalu PXE."""
    fedora = load_option("Fedora", [hd_node(1, ESP_GUID), file_node("\\EFI\\fedora\\shimx64.efi")])
    windows = load_option(
        "Windows Boot Manager",
        [hd_node(1, ESP_GUID), file_node("\\EFI\\Microsoft\\Boot\\bootmgfw.efi")],
        windows_optional_data(),
    )
    pxe = load_option(
        "UEFI: PXE IPv4 Realtek PCIe GBE Family Controller",
        [raw_node(0x02, 0x01, 8), raw_node(0x01, 0x01, 2), raw_node(0x03, 0x0B, 33), raw_node(0x03, 0x0C, 23)],
    )
    return {
        "BootOrder": struct.pack("<3H", 1, 0, 3).hex(),
        "BootCurrent": struct.pack("<H", 0).hex(),
        "Boot0000": windows.hex(),
        "Boot0001": fedora.hex(),
        "Boot0003": pxe.hex(),
    }


# --- Superblock --------------------------------------------------------------


def ext4_image(fs_uuid: str, last_mounted: str = "/boot", label: str = "", write_time: int = 1_760_000_000) -> bytes:
    data = bytearray(128 * 1024)
    sb = 1024
    struct.pack_into("<II", data, sb + 0x2C, write_time - 60, write_time)
    struct.pack_into("<H", data, sb + 0x38, 0xEF53)
    struct.pack_into("<II", data, sb + 0x5C, 0x4, 0x40 | 0x200)
    data[sb + 0x68 : sb + 0x78] = uuid.UUID(fs_uuid).bytes
    data[sb + 0x78 : sb + 0x78 + len(label)] = label.encode()
    data[sb + 0x88 : sb + 0x88 + len(last_mounted)] = last_mounted.encode()
    return bytes(data)


def btrfs_image(fs_uuid: str, label: str = "fedora") -> bytes:
    data = bytearray(128 * 1024)
    base = 0x10000
    data[base + 0x20 : base + 0x30] = uuid.UUID(fs_uuid).bytes
    data[base + 0x40 : base + 0x48] = b"_BHRfS_M"
    data[base + 0x12B : base + 0x12B + len(label)] = label.encode()
    return bytes(data)


def fat32_image(serial: int = 0x1A2B3C4D, label: str = "SYSTEM") -> bytes:
    data = bytearray(128 * 1024)
    data[82:90] = b"FAT32   "
    struct.pack_into("<I", data, 67, serial)
    data[71:82] = label.ljust(11).encode()
    data[510:512] = b"\x55\xaa"
    return bytes(data)


# --- PE / UKI ----------------------------------------------------------------


def pe_with_sections(sections: dict[str, bytes]) -> bytes:
    """PE minimal: header DOS + PE + tabel section (tanpa optional header) + isi section."""
    pe_offset = 0x40
    table_offset = pe_offset + 24
    data_offset = table_offset + 40 * len(sections)
    header = bytearray(data_offset)
    header[0:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, pe_offset)
    header[pe_offset : pe_offset + 4] = b"PE\x00\x00"
    struct.pack_into("<HH", header, pe_offset + 4, 0x8664, len(sections))
    struct.pack_into("<H", header, pe_offset + 20, 0)
    body = b""
    for i, (name, content) in enumerate(sections.items()):
        entry = table_offset + 40 * i
        header[entry : entry + 8] = name.encode().ljust(8, b"\x00")
        struct.pack_into("<IIII", header, entry + 8, len(content), 0x1000 * (i + 1), len(content), data_offset + len(body))
        body += content
    return bytes(header) + body


def grub_core_image(prefix: str | None = None, config: str | None = None) -> bytes:
    """Core image GRUB sintetis dengan blok modul 'mimg' (format grub_module_info64), seperti buatan grub-mkimage."""
    modules = b""
    for kind, text in ((3, prefix), (2, config)):
        if text is None:
            continue
        payload = text.encode() + b"\x00"
        payload += b"\x00" * (-len(payload) % 8)
        modules += struct.pack("<II", kind, 8 + len(payload)) + payload
    info = struct.pack("<IIQQ", 0x676D696D, 0, 24, 24 + len(modules))
    return b"MZ fake GNU GRUB core" + bytes(64) + info + modules


def laptop_real_esp_files() -> dict[str, bytes | str]:
    """Layout ESP laptop asli (dari scan admin 2026-10-21); isi file sintetis kecuali yang disebut."""
    arch_uuid = "77777777-0000-4000-8000-000000000007"  # root Arch yang sudah di-wipe
    return {
        "EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW,
        "EFI\\Boot\\bootx64.efi": SHIM,
        "EFI\\Boot\\BOOTIA32.EFI": b"MZ UEFI SHIM ia32",
        "EFI\\Boot\\fbx64.efi": b"MZ fallback",
        "EFI\\fedora\\shimx64.efi": SHIM,
        "EFI\\fedora\\shimia32.efi": b"MZ UEFI SHIM ia32",
        "EFI\\fedora\\grubx64.efi": b"MZ GNU GRUB signed",
        "EFI\\fedora\\mmx64.efi": b"MZ mok",
        "EFI\\fedora\\BOOTX64.CSV": FEDORA_CSV,
        "EFI\\fedora\\grub.cfg": f"search --no-floppy --root-dev-only --fs-uuid --set=dev {LAPTOP_BOOT_UUID}\n"
        "set prefix=($dev)/grub2\nexport $prefix\nconfigfile $prefix/grub.cfg\n",
        # Prefix asli dari scan: GRUB Arch mencari config di partisi nomor 4 (sekarang Windows Recovery).
        "EFI\\Arch\\grubx64.efi": grub_core_image("(,gpt4)/grub"),
        # Folder grub\ di root ESP: isi asli belum dibaca, jadi config & prefix core.efi di sini sintetis.
        "grub\\grub.cfg": "menuentry 'Arch Linux' {\n"
        f"  search --no-floppy --fs-uuid --set=root {arch_uuid}\n"
        f"  linux /vmlinuz-linux root=UUID={arch_uuid} rw\n}}\n",
        "grub\\grubenv": "# GRUB Environment Block\n",
        "grub\\x86_64-efi\\normal.mod": b"mod",
        "grub\\x86_64-efi\\core.efi": grub_core_image("(,gpt1)/grub"),
        "grub\\locale\\id.mo": b"mo",
        "EFI\\HP\\BIOS\\Current\\08A4F.bin": b"bios image",
        "EFI\\HP\\SystemDiags\\SysDiags.efi": b"MZ HP diagnostics",
        "mach_kernel": b"x" * 34,
        "System\\Library\\CoreServices\\SystemVersion.plist": "<plist/>",
    }


LAPTOP_BOOT_UUID = "5b455826-38e4-4aed-b02b-6a220fa0d2f3"  # partisi #5 asli (ext4, /boot)


# --- ESP palsu ---------------------------------------------------------------


def write_tree(root: Path, files: dict[str, bytes | str]) -> None:
    for rel, content in files.items():
        path = root.joinpath(*rel.split("\\"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def fake_esp(root: Path, inventory: disks.DiskInventory, files: dict[str, bytes | str]):
    write_tree(root, files)
    return scan_esp(root, inventory.esp())


VALID_SIGNATURE = (
    '{"Status":"Valid","Message":"Signature verified.",'
    '"Subject":"CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US",'
    '"Issuer":"CN=Microsoft Windows Production PCA 2011","Thumbprint":"ABC"}'
)


class FakeBcd:
    """Simulasi bcdedit + NVRAM yang punya state: /delete benar-benar menghapus, /copy benar-benar membuat entri.

    Cukup untuk menguji eksekusi & pemulihan tanpa menyentuh firmware sungguhan.
    """

    def __init__(self, bcdedit_text: str, bitlocker: str = None, signature: str = VALID_SIGNATURE):
        from distrorelay.windows.bootentries import BOOTMGR_ID, parse_firmware

        config = parse_firmware(bcdedit_text)
        self.bootmgr = BOOTMGR_ID
        self.entries = {e.identifier: {"description": e.description, "path": e.path} for e in config.entries}
        self.order = list(config.display_order)
        self.numbers = {identifier: n for n, identifier in enumerate(self.entries)}
        self.bitlocker = bitlocker or BITLOCKER_OFF
        self.signature = signature
        self.failures: dict[str, str] = {}  # awalan command_key -> pesan error
        self.calls: list[tuple[str, ...]] = []
        # Objek BCD basi: isi Boot#### di NVRAM berbeda dengan yang dicatat bcdedit (mis. perangkat keras, tanpa path).
        self.nvram_hardware: set[str] = set()
        # Objek yang hanya ada di bcdedit, tanpa Boot####.
        self.bcd_only: set[str] = set()
        # /delete X ikut menghapus Y (meniru Windows yang memasangkan objek ke Boot#### lain).
        self.collateral: dict[str, str] = {}
        # Tabel partisi (format output DISKS_SCRIPT), berubah lewat script hapus/buat ulang partisi & perluas C:.
        self.disks = json.loads(fixture_text("disks_laptop.json"))

    def add(self, identifier: str, description: str, path: str | None, *, in_order: bool = True) -> None:
        self.entries[identifier] = {"description": description, "path": path}
        self.numbers[identifier] = max(self.numbers.values()) + 1
        if in_order:
            self.order.append(identifier)

    # --- Runner ---
    def run(self, args, *, timeout=120):
        from distrorelay.windows.runner import CommandResult, command_key, decode_powershell

        args = tuple(args)
        self.calls.append(args)
        key = command_key(args)

        def ok(stdout=""):
            return CommandResult(args, 0, stdout, "")

        for prefix, error in self.failures.items():
            if key.startswith(prefix):
                return CommandResult(args, 1, "", error)
        script = decode_powershell(args)
        if script is not None:
            partition_scripts = {
                "ps:delete-partition": self._delete_partition,
                "ps:restore-partition": self._restore_partition,
                "ps:extend-c": self._extend_c,
            }
            if key in partition_scripts:
                error = partition_scripts[key](script)
                return CommandResult(args, 1, "", error) if error else ok()
            return ok({"ps:disks": json.dumps(self.disks), "ps:bitlocker": self.bitlocker,
                       "ps:authenticode": self.signature}.get(key, ""))
        if key.startswith("mountvol"):
            return ok()
        a = list(args[1:])
        low = [x.lower() for x in a]
        if low == ["/enum", "firmware", "/v"] or (low[:1] == ["/store"] and "/enum" in low):
            return ok(self.render())
        if low[:1] == ["/export"]:
            Path(a[1]).write_bytes(b"regf hive palsu")
            Path(a[1] + ".LOG").write_bytes(b"log")
            return ok("The operation completed successfully.")
        if low == ["/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"]:
            self.order = [self.bootmgr] + [i for i in self.order if i != self.bootmgr]
            return ok("The operation completed successfully.")
        if low[:1] == ["/delete"] and len(low) == 2:
            if low[1] not in self.entries:
                return CommandResult(args, 1, "", "The specified entry identifier is not valid.")
            for gone in {low[1], self.collateral.get(low[1])} - {None}:
                self.entries.pop(gone, None)
                self.order = [i for i in self.order if i != gone]
            return ok("The operation completed successfully.")
        if low[:3] == ["/copy", "{bootmgr}", "/d"]:
            new = "{" + str(uuid.uuid4()) + "}"
            self.entries[new] = {"description": a[3], "path": self.entries[self.bootmgr]["path"]}
            self.numbers[new] = max(self.numbers.values()) + 1
            return ok(f"The entry was successfully copied to {new}.")
        if low[:1] == ["/set"] and len(low) == 4 and low[2] == "path":
            self.entries[low[1]]["path"] = a[3]
            return ok("The operation completed successfully.")
        if low[:3] == ["/set", "{fwbootmgr}", "displayorder"] and low[4:] == ["/addlast"]:
            self.order = [i for i in self.order if i != low[3]] + [low[3]]
            return ok("The operation completed successfully.")
        return CommandResult(args, 1, "", f"FakeBcd: perintah tidak dikenal {key!r}")

    # --- Partisi (meniru Remove-Partition / New-Partition / Resize-Partition) ---
    def partition(self, guid: str) -> dict | None:
        wanted = "{" + guid.strip("{}").lower() + "}"
        return next((p for p in self.disks["Partitions"] if p["Guid"].lower() == wanted), None)

    def _delete_partition(self, script: str) -> str | None:
        guid = re.search(r"\$_\.Guid -eq '(\{[0-9a-f-]+\})'", script).group(1)
        part = self.partition(guid)
        if part is None:
            return f"Partisi {guid} tidak ditemukan"
        self.disks["Partitions"].remove(part)
        return None

    def _restore_partition(self, script: str) -> str | None:
        offset = int(re.search(r"-Offset (\d+)", script).group(1))
        size = int(re.search(r"-Size (\d+)", script).group(1))
        gpt_type = re.search(r"-GptType '(\{[0-9a-f-]+\})'", script).group(1)
        parts = self.disks["Partitions"]
        if any(p["Offset"] < offset + size and p["Offset"] + p["Size"] > offset for p in parts):
            return "Ruangnya sudah dipakai partisi lain"
        used = {p["PartitionNumber"] for p in parts}
        number = next(n for n in range(1, 129) if n not in used)  # slot GPT kosong pertama, seperti Windows
        parts.append({
            "DiskNumber": 0, "PartitionNumber": number, "Guid": "{" + str(uuid.uuid4()) + "}", "GptType": gpt_type,
            "Offset": offset, "Size": size, "DriveLetter": "\x00", "AccessPaths": [None], "IsBoot": False,
            "IsSystem": False, "IsHidden": False, "Type": "Unknown", "FileSystem": None, "FileSystemLabel": None,
        })
        return None

    def _extend_c(self, script: str) -> str | None:
        c = next(p for p in self.disks["Partitions"] if p["DriveLetter"] == "C")
        end = c["Offset"] + c["Size"]
        later = [p["Offset"] for p in self.disks["Partitions"] if p["Offset"] >= end]
        limit = min(later) if later else self.disks["Disks"][0]["Size"] - 2**20
        if limit <= end:
            return "Tidak ada ruang kosong tepat sesudah C:"
        c["Size"] = limit - c["Offset"]
        return None

    # --- Tampilan seperti bcdedit /enum firmware /v ---
    def render(self) -> str:
        lines = ["", "Firmware Boot Manager", "---------------------",
                 "identifier              {a5a30fa2-3d06-4e9f-b5f4-a01df9d1fcba}"]
        for n, identifier in enumerate(self.order):
            lines.append(("displayorder            " if n == 0 else "                        ") + identifier)
        lines.append("timeout                 0")
        for identifier, entry in self.entries.items():
            title = "Windows Boot Manager" if identifier == self.bootmgr else "Firmware Application (101fffff)"
            lines += ["", title, "-" * len(title), f"identifier              {identifier}"]
            if entry["path"]:
                lines.append("device                  partition=\\Device\\HarddiskVolume1")
                lines.append(f"path                    {entry['path']}")
            lines.append(f"description             {entry['description']}")
        return "\n".join(lines) + "\n"

    def nvram(self):
        order = [i for i in self.order if i not in self.bcd_only]
        raw = {"BootOrder": struct.pack(f"<{len(order)}H", *(self.numbers[i] for i in order)).hex()}
        for identifier, entry in self.entries.items():
            if identifier in self.bcd_only:
                continue
            if identifier in self.nvram_hardware:  # Acpi/Pci/Sata, seperti "UEFI VBOX HARDDISK"
                nodes = [raw_node(0x02, 0x01, 8), raw_node(0x01, 0x01, 2), raw_node(0x03, 0x12, 6)]
            else:
                nodes = [hd_node(1, ESP_GUID)] + ([file_node(entry["path"])] if entry["path"] else [])
            optional = windows_optional_data(identifier) if identifier == self.bootmgr else b""
            raw[f"Boot{self.numbers[identifier]:04X}"] = load_option(entry["description"], nodes, optional).hex()
        return snapshot_from_raw(raw)


def laptop_world(tmp_path: Path):
    """Laptop simulasi: bcdedit asli dari fixture, ESP dengan layout asli, partisi #5 = ext4 /boot Fedora."""
    from distrorelay.windows.runner import ReadOnlyRunner
    from distrorelay.windows.scanner import ScanDeps

    bcd = FakeBcd(fixture_text("bcdedit_firmware_laptop.txt"))
    esp_root = tmp_path / "esp"
    write_tree(esp_root, laptop_real_esp_files())

    def read_head(part, sector_size):
        return {"disk0-p5": ext4_image(LAPTOP_BOOT_UUID), "disk0-p1": fat32_image()}.get(part.id, bytes(4096))

    def deps():
        return ScanDeps(ReadOnlyRunner(bcd), lambda: True, bcd.nvram, read_head, fake_mount(esp_root))

    return bcd, esp_root, deps


BITLOCKER_OFF = '[{"DriveLetter":"C:","ProtectionStatus":0,"ConversionStatus":0,"EncryptionMethod":0,"VolumeType":0}]'
BITLOCKER_ON = '[{"DriveLetter":"C:","ProtectionStatus":1,"ConversionStatus":1,"EncryptionMethod":7,"VolumeType":0}]'


def fake_mount(root: Path):
    @contextmanager
    def mount(runner, esp):
        yield MountedEsp(root, "Z", temporary=True)

    return mount


def scan_deps(esp_root: Path, admin: bool = True, files=None, bitlocker: str = BITLOCKER_ON):
    """ScanDeps palsu: FakeRunner + ESP di folder temp + NVRAM/superblock sintetis."""
    from distrorelay.windows.scanner import ScanDeps

    fake = FakeRunner()
    fake.add("ps:disks", fixture_text("disks_laptop.json"))
    fake.add("bcdedit /enum firmware /v", fixture_text("bcdedit_firmware_synthetic_en.txt"))
    fake.add("ps:bitlocker", bitlocker)
    write_tree(esp_root, files if files is not None else laptop_esp_files())

    def read_head(part, sector_size):
        return {"disk0-p5": ext4_image(BOOT_UUID), "disk0-p1": fat32_image()}.get(part.id, bytes(4096))

    deps = ScanDeps(
        runner=ReadOnlyRunner(fake),
        is_admin=lambda: admin,
        read_nvram=lambda: snapshot_from_raw(nvram_raw_laptop()),
        read_partition_head=read_head,
        mount_esp=fake_mount(esp_root),
    )
    return deps, fake


BOOTMGFW = b"MZ fake Windows Boot Manager Microsoft Corporation"
SHIM = b"MZ fake UEFI SHIM signed by Microsoft Corporation UEFI CA 2011"

FEDORA_STUB = f"""search --no-floppy --root-dev-only --fs-uuid --set=dev {BOOT_UUID}
set prefix=($dev)/grub2

export $prefix
configfile $prefix/grub.cfg
"""

FEDORA_CSV = "﻿shimx64.efi,Fedora,,This is the boot entry for Fedora\n".encode("utf-16-le")


def laptop_esp_files() -> dict[str, bytes | str]:
    """ESP seperti dugaan kondisi laptop: Windows + sisa Fedora (shim, GRUB, stub, CSV, fallback shim)."""
    return {
        "EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW,
        "EFI\\Microsoft\\Boot\\BCD": b"bcd",
        "EFI\\Boot\\bootx64.efi": SHIM,
        "EFI\\Boot\\fbx64.efi": b"MZ fallback",
        "EFI\\fedora\\shimx64.efi": SHIM,
        "EFI\\fedora\\grubx64.efi": b"MZ GNU GRUB",
        "EFI\\fedora\\mmx64.efi": b"MZ mok",
        "EFI\\fedora\\BOOTX64.CSV": FEDORA_CSV,
        "EFI\\fedora\\grub.cfg": FEDORA_STUB,
    }


def gpt_slots_like_windows(inventory: disks.DiskInventory) -> dict[int, dict[int, str]]:
    """Tabel slot GPT tanpa slot kosong (nomor slot = nomor partisi Windows), seperti disk laptop asli."""
    slots: dict[int, dict[int, str]] = {}
    for part in inventory.partitions:
        slots.setdefault(part.disk_number, {})[part.number] = part.guid
    return slots
