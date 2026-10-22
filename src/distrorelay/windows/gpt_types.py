"""Tabel GPT partition type GUID dan kategorinya.

Kategori `linux` adalah whitelist: hanya partisi dengan tipe ini yang boleh dipilih untuk dihapus (M3),
dan hanya tipe ini yang bisa dibuat ulang lewat Pulihkan. Kategori di `PROTECTED_CATEGORIES` selalu dikunci.
"""

from __future__ import annotations

from dataclasses import dataclass

LINUX = "linux"

ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
WINDOWS_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
LINUX_FILESYSTEM = "0fc63daf-8483-4772-8e79-3d69d8477de4"
LINUX_ROOT_X86_64 = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
LINUX_HOME = "933ac7e1-2eb4-4f13-b844-0e14e2aef915"
XBOOTLDR = "bc13c2ff-59e6-4262-a352-b275fd6f7172"
LINUX_SWAP = "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
LINUX_LVM = "e6d6d379-f507-44c2-a23c-238f2a3df928"
LINUX_LUKS = "ca7d7ccb-63ed-4c53-861c-1742536059cc"
LINUX_RAID = "a19d880f-05fc-4d3b-a006-743f0f84911e"


@dataclass(frozen=True)
class GptType:
    guid: str
    name: str
    category: str


_TABLE = (
    GptType(ESP, "EFI System Partition (ESP)", "esp"),
    GptType(MSR, "Microsoft Reserved (MSR)", "msr"),
    GptType(BASIC_DATA, "Basic data (NTFS/FAT/exFAT)", "basic"),
    GptType(WINDOWS_RECOVERY, "Windows Recovery", "recovery"),
    GptType("5808c8aa-7e8f-42e0-85d2-e1e90434cfb3", "Windows LDM metadata", "windows-other"),
    GptType("af9b60a0-1431-4f62-bc68-3311714a69ad", "Windows LDM data", "windows-other"),
    GptType("e75caf8f-f680-4cee-afa3-b001e56efc2d", "Windows Storage Spaces", "windows-other"),
    GptType(LINUX_FILESYSTEM, "Linux filesystem", LINUX),
    GptType(LINUX_ROOT_X86_64, "Linux root (x86-64)", LINUX),
    GptType(LINUX_HOME, "Linux /home", LINUX),
    GptType(XBOOTLDR, "Linux extended boot (XBOOTLDR)", LINUX),
    GptType(LINUX_SWAP, "Linux swap", LINUX),
    GptType(LINUX_LVM, "Linux LVM", LINUX),
    GptType(LINUX_LUKS, "LUKS (Linux terenkripsi)", LINUX),
    GptType(LINUX_RAID, "Linux RAID", LINUX),
    GptType("21686148-6449-6e6f-744e-656564454649", "BIOS boot (GRUB legacy)", "other"),
)

TYPES = {t.guid: t for t in _TABLE}

PROTECTED_CATEGORIES = frozenset({"esp", "msr", "basic", "recovery", "windows-other"})


def normalize_guid(value: str | None) -> str | None:
    """'{ABC-...}' -> 'abc-...'. None/kosong -> None."""
    if not value:
        return None
    cleaned = value.strip().strip("{}").strip().lower()
    return cleaned or None


def lookup(guid: str | None) -> GptType:
    normalized = normalize_guid(guid)
    if normalized is None:
        return GptType("", "Tanpa tipe GPT", "unknown")
    return TYPES.get(normalized, GptType(normalized, "Tidak dikenal", "unknown"))
