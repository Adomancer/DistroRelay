import struct
import uuid

from distrohop.windows.fsprobe import probe_bytes
from helpers import BOOT_UUID, btrfs_image, ext4_image, fat32_image

ROOT_UUID = "11111111-2222-4333-8444-555555555555"


def test_ext4_with_last_mounted_path():
    info = probe_bytes(ext4_image(BOOT_UUID, last_mounted="/boot", label="boot"))
    assert info.fs_type == "ext4"
    assert info.uuid == BOOT_UUID
    assert info.label == "boot"
    assert info.last_mounted == "/boot"
    assert info.write_time.startswith("2025-")
    assert "terakhir di-mount di /boot" in info.describe()


def test_btrfs():
    info = probe_bytes(btrfs_image(ROOT_UUID, label="fedora"))
    assert (info.fs_type, info.uuid, info.label) == ("btrfs", ROOT_UUID, "fedora")


def test_fat32():
    info = probe_bytes(fat32_image(0x1A2B3C4D, "SYSTEM"))
    assert (info.fs_type, info.uuid, info.label) == ("vfat", "1A2B-3C4D", "SYSTEM")


def test_swap():
    data = bytearray(128 * 1024)
    data[4086:4096] = b"SWAPSPACE2"
    data[1036:1052] = uuid.UUID(ROOT_UUID).bytes
    info = probe_bytes(bytes(data))
    assert (info.fs_type, info.uuid) == ("swap", ROOT_UUID)


def test_luks2_and_xfs():
    data = bytearray(128 * 1024)
    data[:6] = b"LUKS\xba\xbe"
    struct.pack_into(">H", data, 6, 2)
    data[24:30] = b"cryptd"
    data[168 : 168 + 36] = ROOT_UUID.encode()
    luks = probe_bytes(bytes(data))
    assert (luks.fs_type, luks.uuid, luks.label, luks.version) == ("crypto_LUKS", ROOT_UUID, "cryptd", "2")

    data = bytearray(128 * 1024)
    data[:4] = b"XFSB"
    data[32:48] = uuid.UUID(ROOT_UUID).bytes
    data[108:112] = b"data"
    xfs = probe_bytes(bytes(data))
    assert (xfs.fs_type, xfs.uuid, xfs.label) == ("xfs", ROOT_UUID, "data")


def test_ntfs_and_unknown():
    data = bytearray(4096)
    data[3:11] = b"NTFS    "
    struct.pack_into("<Q", data, 0x48, 0x0123456789ABCDEF)
    assert probe_bytes(bytes(data)).uuid == "0123456789ABCDEF"
    assert probe_bytes(bytes(128 * 1024)) is None
