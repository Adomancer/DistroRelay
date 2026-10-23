"""Kenali isi partisi dari superblock-nya (read-only).

Windows tidak bisa membaca ext4/btrfs, tapi header filesystem ada di posisi tetap di awal partisi.
Dari situ kita dapat jenis filesystem, label, UUID, dan untuk ext4 juga *last-mounted path*,
yang menunjukkan partisi itu dulunya `/`, `/boot`, `/home`, dst.

Disk mentah hanya dibuka dengan mode "rb". Byte mentah tidak pernah disimpan ke laporan
(header LUKS berisi metadata keyslot), hanya hasil parse-nya.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

PROBE_SIZE = 128 * 1024


@dataclass
class FsInfo:
    fs_type: str
    uuid: str | None = None
    label: str | None = None
    last_mounted: str | None = None
    mount_time: str | None = None
    write_time: str | None = None
    version: str | None = None

    def describe(self) -> str:
        parts = [self.fs_type]
        if self.label:
            parts.append(f"label '{self.label}'")
        if self.uuid:
            parts.append(f"UUID {self.uuid}")
        if self.last_mounted:
            parts.append(f"terakhir di-mount di {self.last_mounted}")
        if self.write_time:
            parts.append(f"terakhir ditulis {self.write_time[:10]}")
        return ", ".join(parts)


def _text(raw: bytes) -> str | None:
    value = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    return value or None


def _timestamp(value: int) -> str | None:
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")


def _fat_serial(value: int) -> str:
    return f"{value >> 16:04X}-{value & 0xFFFF:04X}"


def _luks(data: bytes) -> FsInfo | None:
    if data[:6] != b"LUKS\xba\xbe" or len(data) < 208:
        return None
    version = struct.unpack_from(">H", data, 6)[0]
    label = _text(data[24:72]) if version == 2 else None
    return FsInfo("crypto_LUKS", uuid=_text(data[168:208]), label=label, version=str(version))


def _lvm2(data: bytes) -> FsInfo | None:
    if len(data) < 1024 or data[512:520] != b"LABELONE" or data[536:544] != b"LVM2 001":
        return None
    header_offset = struct.unpack_from("<I", data, 512 + 20)[0]
    raw = data[512 + header_offset : 512 + header_offset + 32].decode("ascii", "replace")
    pv_uuid = "-".join((raw[0:6], raw[6:10], raw[10:14], raw[14:18], raw[18:22], raw[22:26], raw[26:32]))
    return FsInfo("LVM2_member", uuid=pv_uuid)


def _xfs(data: bytes) -> FsInfo | None:
    if data[:4] != b"XFSB" or len(data) < 120:
        return None
    return FsInfo("xfs", uuid=str(uuid.UUID(bytes=data[32:48])), label=_text(data[108:120]))


def _ext(data: bytes) -> FsInfo | None:
    if len(data) < 2048:
        return None
    sb = data[1024:2048]
    if struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
        return None
    compat, incompat = struct.unpack_from("<II", sb, 0x5C)
    if incompat & (0x40 | 0x80 | 0x200):  # extents, 64bit, flex_bg
        fs_type = "ext4"
    elif compat & 0x4:  # has_journal
        fs_type = "ext3"
    else:
        fs_type = "ext2"
    mount_time, write_time = struct.unpack_from("<II", sb, 0x2C)
    return FsInfo(
        fs_type,
        uuid=str(uuid.UUID(bytes=sb[0x68:0x78])),
        label=_text(sb[0x78:0x88]),
        last_mounted=_text(sb[0x88:0xC8]),
        mount_time=_timestamp(mount_time),
        write_time=_timestamp(write_time),
    )


def _btrfs(data: bytes) -> FsInfo | None:
    base = 0x10000
    if len(data) < base + 0x22B or data[base + 0x40 : base + 0x48] != b"_BHRfS_M":
        return None
    return FsInfo(
        "btrfs",
        uuid=str(uuid.UUID(bytes=data[base + 0x20 : base + 0x30])),
        label=_text(data[base + 0x12B : base + 0x22B]),
    )


def _swap(data: bytes) -> FsInfo | None:
    if len(data) < 4096 or data[4086:4096] not in (b"SWAPSPACE2", b"SWAP-SPACE"):
        return None
    return FsInfo("swap", uuid=str(uuid.UUID(bytes=data[1036:1052])), label=_text(data[1052:1068]))


def _bitlocker(data: bytes) -> FsInfo | None:
    return FsInfo("BitLocker") if data[3:11] == b"-FVE-FS-" else None


def _ntfs(data: bytes) -> FsInfo | None:
    if data[3:11] != b"NTFS    " or len(data) < 0x50:
        return None
    return FsInfo("ntfs", uuid=f"{struct.unpack_from('<Q', data, 0x48)[0]:016X}")


def _exfat(data: bytes) -> FsInfo | None:
    if data[3:11] != b"EXFAT   " or len(data) < 104:
        return None
    return FsInfo("exfat", uuid=_fat_serial(struct.unpack_from("<I", data, 100)[0]))


def _vfat(data: bytes) -> FsInfo | None:
    if len(data) < 512 or data[510:512] != b"\x55\xaa":
        return None
    if data[82:90] == b"FAT32   ":
        serial, label = struct.unpack_from("<I", data, 67)[0], data[71:82]
    elif data[54:57] == b"FAT":
        serial, label = struct.unpack_from("<I", data, 39)[0], data[43:54]
    else:
        return None
    text = label.decode("ascii", "replace").strip()
    return FsInfo("vfat", uuid=_fat_serial(serial), label=None if text in ("", "NO NAME") else text)


_PROBES = (_luks, _lvm2, _xfs, _ext, _btrfs, _swap, _bitlocker, _ntfs, _exfat, _vfat)


def probe_bytes(data: bytes) -> FsInfo | None:
    """Kenali filesystem dari byte awal partisi. None kalau tidak dikenali."""
    for probe in _PROBES:
        info = probe(data)
        if info is not None:
            return info
    return None


_GPT_SIGNATURE = b"EFI PART"
_MAX_GPT_ENTRIES = 1024


def parse_gpt_slots(header: bytes, entries: bytes) -> dict[int, str]:
    """Nomor slot GPT (1-based, seperti `gptN` di GRUB) -> GUID unik partisi, dari header + array entri GPT.

    Nomor partisi Windows tidak selalu sama dengan nomor slot: slot kosong di tengah (bekas partisi yang dihapus)
    dilewati Windows tapi tidak oleh GRUB.
    """
    if len(header) < 92 or header[:8] != _GPT_SIGNATURE:
        raise ValueError("Header GPT tidak dikenali")
    header_size, header_crc = struct.unpack_from("<II", header, 12)
    count, size, array_crc = struct.unpack_from("<III", header, 80)
    _check_entry_layout(count, size)
    if not 92 <= header_size <= len(header):
        raise ValueError(f"Ukuran header GPT tidak wajar ({header_size})")
    zeroed = header[:16] + b"\0\0\0\0" + header[20:header_size]
    if zlib.crc32(zeroed) & 0xFFFFFFFF != header_crc:
        raise ValueError("CRC header GPT tidak cocok")
    if len(entries) < count * size:
        raise ValueError(f"Array entri GPT terpotong ({len(entries)} dari {count * size} byte)")
    if zlib.crc32(entries[: count * size]) & 0xFFFFFFFF != array_crc:
        raise ValueError("CRC array entri GPT tidak cocok")
    slots = {}
    for index in range(count):
        entry = entries[index * size : index * size + 128]
        if entry[:16] == b"\0" * 16:  # tipe kosong = slot tidak dipakai
            continue
        slots[index + 1] = "{" + str(uuid.UUID(bytes_le=entry[16:32])) + "}"
    return slots


def _check_entry_layout(count: int, size: int) -> None:
    if not 0 < count <= _MAX_GPT_ENTRIES or not 128 <= size <= 4096 or size % 128:
        raise ValueError(f"Header GPT tidak wajar ({count} entri x {size} byte)")


def read_gpt_slots(disk_number: int, sector_size: int = 512) -> dict[int, str]:
    """Baca header GPT (LBA 1) dan array entrinya langsung dari disk fisik (read-only, butuh Administrator)."""
    with open(rf"\\.\PhysicalDrive{disk_number}", "rb", buffering=0) as disk:
        disk.seek(sector_size)
        header = disk.read(sector_size)
        if header[:8] != _GPT_SIGNATURE:
            raise ValueError(f"Disk {disk_number} tidak punya header GPT")
        start_lba = struct.unpack_from("<Q", header, 72)[0]
        count, size = struct.unpack_from("<II", header, 80)
        _check_entry_layout(count, size)  # sebelum membaca: ukuran rusak tidak boleh meminta baca raksasa
        length = count * size
        length += (-length) % sector_size
        disk.seek(start_lba * sector_size)
        entries = disk.read(length)
    return parse_gpt_slots(header, entries)


def read_partition_head(
    disk_number: int, offset: int, partition_size: int, sector_size: int = 512, size: int = PROBE_SIZE
) -> bytes:
    """Baca byte awal partisi langsung dari disk fisik (butuh Administrator)."""
    length = min(size, partition_size)
    length -= length % sector_size
    with open(rf"\\.\PhysicalDrive{disk_number}", "rb", buffering=0) as disk:
        disk.seek(offset)
        return disk.read(length)
