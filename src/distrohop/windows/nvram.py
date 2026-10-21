"""Baca entri boot langsung dari NVRAM UEFI dan parse EFI_LOAD_OPTION.

Hanya MEMBACA (GetFirmwareEnvironmentVariableW). Modul ini sengaja tidak punya fungsi tulis:
perubahan entri boot di DistroHop selalu lewat bcdedit.
"""

from __future__ import annotations

import re
import struct
import sys
import uuid
from dataclasses import dataclass, field

EFI_GLOBAL_VARIABLE = "{8BE4DF61-93CA-11D2-AA0D-00E098032B8C}"
LOAD_OPTION_ACTIVE = 0x00000001
LOAD_OPTION_HIDDEN = 0x00000008

_BCD_OBJECT = re.compile(r"BCDOBJECT=\{([0-9a-fA-F-]{36})\}")

_NODE_NAMES = {
    (0x01, 0x01): "Pci",
    (0x01, 0x04): "VenHw",
    (0x02, 0x01): "Acpi",
    (0x03, 0x01): "Ata",
    (0x03, 0x02): "Scsi",
    (0x03, 0x05): "USB",
    (0x03, 0x0B): "MAC",
    (0x03, 0x0C): "IPv4",
    (0x03, 0x0D): "IPv6",
    (0x03, 0x12): "Sata",
    (0x03, 0x17): "NVMe",
    (0x03, 0x18): "URI",
    (0x03, 0x1A): "SD",
    (0x03, 0x1D): "eMMC",
    (0x04, 0x02): "CDROM",
    (0x04, 0x03): "VenMedia",
    (0x04, 0x06): "FvFile",
    (0x04, 0x07): "Fv",
}


class NvramError(RuntimeError):
    pass


@dataclass
class DevicePathNode:
    type: int
    subtype: int
    data: bytes

    @property
    def is_end(self) -> bool:
        return self.type == 0x7F

    @property
    def file_text(self) -> str | None:
        if (self.type, self.subtype) != (0x04, 0x04):
            return None
        return self.data.decode("utf-16-le", "replace").split("\x00", 1)[0]

    def hard_drive(self) -> tuple[int, str | None] | None:
        """(nomor partisi, GUID partisi) untuk node HD dengan tanda tangan GPT."""
        if (self.type, self.subtype) != (0x04, 0x01) or len(self.data) < 38:
            return None
        number = struct.unpack_from("<I", self.data, 0)[0]
        signature, signature_type = self.data[20:36], self.data[37]
        if signature_type == 0x02:
            return number, str(uuid.UUID(bytes_le=signature))
        return number, None

    def describe(self) -> str:
        hd = self.hard_drive()
        if hd is not None:
            number, guid = hd
            return f"HD({number},GPT,{guid})" if guid else f"HD({number})"
        if (self.type, self.subtype) == (0x04, 0x04):
            return f"File({self.file_text})"
        if (self.type, self.subtype) == (0x05, 0x01) and len(self.data) > 4:
            text = self.data[4:].split(b"\x00", 1)[0].decode("ascii", "replace")
            return f"BBS({text})"
        return _NODE_NAMES.get((self.type, self.subtype), f"Path({self.type},{self.subtype})")


def parse_device_path(data: bytes) -> list[DevicePathNode]:
    """Semua node di FilePathList. Bisa berisi lebih dari satu device path, masing-masing diakhiri End (7F FF)."""
    nodes: list[DevicePathNode] = []
    offset = 0
    while offset + 4 <= len(data):
        node_type, subtype, length = struct.unpack_from("<BBH", data, offset)
        if length < 4 or offset + length > len(data):
            break
        nodes.append(DevicePathNode(node_type, subtype, data[offset + 4 : offset + length]))
        offset += length
    return nodes


@dataclass
class LoadOption:
    number: int
    attributes: int
    description: str
    nodes: list[DevicePathNode]
    optional_data: bytes
    raw: bytes

    @property
    def label(self) -> str:
        return f"Boot{self.number:04X}"

    @property
    def active(self) -> bool:
        return bool(self.attributes & LOAD_OPTION_ACTIVE)

    @property
    def hidden(self) -> bool:
        return bool(self.attributes & LOAD_OPTION_HIDDEN)

    @property
    def partition_guid(self) -> str | None:
        for node in self.nodes:
            hd = node.hard_drive()
            if hd and hd[1]:
                return hd[1]
        return None

    @property
    def file_path(self) -> str | None:
        parts = [n.file_text for n in self.nodes if n.file_text]
        if not parts:
            return None
        path = ""
        for part in parts:
            if path and not path.endswith("\\") and not part.startswith("\\"):
                path += "\\"
            path += part
        path = path.replace("/", "\\")
        return path if path.startswith("\\") else "\\" + path

    @property
    def bcd_object(self) -> str | None:
        """GUID objek BCD yang ditulis Windows di optional data (hanya untuk entri milik Windows)."""
        for start in (0, 1):
            text = self.optional_data[start:].decode("utf-16-le", "ignore")
            match = _BCD_OBJECT.search(text)
            if match:
                return "{" + match.group(1).lower() + "}"
        return None

    @property
    def device_summary(self) -> str:
        paths: list[list[str]] = [[]]
        for node in self.nodes:
            if node.is_end:
                if node.subtype == 0xFF and paths[-1]:
                    paths.append([])
                continue
            paths[-1].append(node.describe())
        return " | ".join("/".join(p) for p in paths if p)


def _layout(raw: bytes) -> tuple[int, str, int, int]:
    """(attributes, description, awal device path, panjang device path) dari EFI_LOAD_OPTION."""
    if len(raw) < 8:
        raise ValueError("EFI_LOAD_OPTION terlalu pendek")
    attributes, path_length = struct.unpack_from("<IH", raw, 0)
    end = 6
    while end + 1 < len(raw) and raw[end : end + 2] != b"\x00\x00":
        end += 2
    path_start = end + 2
    if path_start + path_length > len(raw):
        raise ValueError("FilePathList melewati akhir data")
    return attributes, raw[6:end].decode("utf-16-le", "replace"), path_start, path_length


def parse_load_option(number: int, raw: bytes) -> LoadOption:
    attributes, description, path_start, path_length = _layout(raw)
    device_path = raw[path_start : path_start + path_length]
    optional = raw[path_start + path_length :]
    return LoadOption(number, attributes, description, parse_device_path(device_path), optional, raw)


# Node device path yang bisa memuat identitas perangkat: (type, subtype) -> rentang byte payload
# yang dinolkan. Panjang node tidak berubah, jadi hasilnya tetap bisa di-parse.
_IDENTIFYING = {
    (0x01, 0x04): (16, None),  # VenHw: GUID vendor dipertahankan, sisanya (mis. nomor seri SSD) dinolkan
    (0x03, 0x0A): (16, None),  # VenMsg
    (0x04, 0x03): (16, None),  # VenMedia
    (0x03, 0x17): (4, 12),  # NVMe: EUI-64 namespace (identik dengan nomor seri versi Windows)
    (0x03, 0x0B): (0, 32),  # MAC address
    (0x03, 0x10): (4, None),  # USB WWID: nomor seri
}


def redact_load_option(raw: bytes) -> bytes:
    """Salinan EFI_LOAD_OPTION dengan nomor seri/EUI/MAC dinolkan. Hanya untuk laporan yang dibagikan."""
    try:
        _, _, path_start, path_length = _layout(raw)
    except ValueError:
        return raw
    data = bytearray(raw)
    offset, end = path_start, path_start + path_length
    while offset + 4 <= end:
        node_type, subtype, length = struct.unpack_from("<BBH", data, offset)
        if length < 4 or offset + length > end:
            break
        rule = _IDENTIFYING.get((node_type, subtype))
        if rule:
            start, stop = rule
            payload_end = length - 4 if stop is None else min(stop, length - 4)
            for i in range(offset + 4 + start, offset + 4 + payload_end):
                data[i] = 0
        offset += length
    return bytes(data)


def redact_raw(raw: dict[str, str]) -> dict[str, str]:
    return {
        name: redact_load_option(bytes.fromhex(value)).hex() if re.fullmatch(r"Boot[0-9A-Fa-f]{4}", name) else value
        for name, value in raw.items()
    }


@dataclass
class NvramSnapshot:
    boot_order: list[int]
    boot_current: int | None
    boot_next: int | None
    options: dict[int, LoadOption]
    raw: dict[str, str] = field(default_factory=dict)  # nama variabel -> hex, untuk backup mentah
    errors: list[str] = field(default_factory=list)

    def position(self, number: int) -> int | None:
        return self.boot_order.index(number) if number in self.boot_order else None


def snapshot_from_raw(raw: dict[str, str]) -> NvramSnapshot:
    """Bangun snapshot dari dump hex (dipakai test & untuk membaca ulang backup)."""
    order_bytes = bytes.fromhex(raw.get("BootOrder", ""))
    boot_order = list(struct.unpack(f"<{len(order_bytes) // 2}H", order_bytes[: len(order_bytes) // 2 * 2]))

    def u16(name: str) -> int | None:
        value = bytes.fromhex(raw[name]) if name in raw else b""
        return struct.unpack("<H", value[:2])[0] if len(value) >= 2 else None

    options: dict[int, LoadOption] = {}
    errors: list[str] = []
    for name, value in raw.items():
        match = re.fullmatch(r"Boot([0-9A-Fa-f]{4})", name)
        if not match:
            continue
        number = int(match.group(1), 16)
        try:
            options[number] = parse_load_option(number, bytes.fromhex(value))
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
    return NvramSnapshot(boot_order, u16("BootCurrent"), u16("BootNext"), options, dict(raw), errors)


# --- Pembacaan dari Windows --------------------------------------------------

_ERROR_MESSAGES = {
    1: "Firmware tidak menyediakan variabel UEFI (mode Legacy BIOS?)",
    5: "Akses ditolak",
    1314: "Hak SeSystemEnvironmentPrivilege tidak aktif (jalankan sebagai Administrator)",
}


def _enable_privilege(name: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

    token_adjust_privileges, token_query, se_privilege_enabled = 0x20, 0x08, 0x02
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_adjust_privileges | token_query, ctypes.byref(token)
    ):
        raise NvramError(f"OpenProcessToken gagal (error {ctypes.get_last_error()})")
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            raise NvramError(f"LookupPrivilegeValue gagal (error {ctypes.get_last_error()})")
        privileges = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, se_privilege_enabled)))
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None):
            raise NvramError(f"AdjustTokenPrivileges gagal (error {ctypes.get_last_error()})")
        if ctypes.get_last_error() == 1300:  # ERROR_NOT_ALL_ASSIGNED
            raise NvramError(_ERROR_MESSAGES[1314])
    finally:
        kernel32.CloseHandle(token)


def _get_variable(name: str, missing_ok: bool = False) -> bytes | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    getter = kernel32.GetFirmwareEnvironmentVariableW
    getter.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD]
    getter.restype = wintypes.DWORD
    buffer = ctypes.create_string_buffer(64 * 1024)
    length = getter(name, EFI_GLOBAL_VARIABLE, buffer, len(buffer))
    if length == 0:
        error = ctypes.get_last_error()
        if error == 203 and missing_ok:  # ERROR_ENVVAR_NOT_FOUND
            return None
        message = _ERROR_MESSAGES.get(error, f"error Windows {error}")
        raise NvramError(f"Gagal membaca {name}: {message}")
    return buffer.raw[:length]


def read_nvram(scan_range: int = 0x100) -> NvramSnapshot:
    """Baca BootOrder, BootCurrent, BootNext, dan semua Boot#### (yang ada di BootOrder + 0000..00FF)."""
    if sys.platform != "win32":
        raise NvramError("Pembacaan NVRAM hanya didukung di Windows")
    _enable_privilege("SeSystemEnvironmentPrivilege")
    raw: dict[str, str] = {}
    order = _get_variable("BootOrder", missing_ok=True) or b""
    raw["BootOrder"] = order.hex()
    for name in ("BootCurrent", "BootNext"):
        value = _get_variable(name, missing_ok=True)
        if value is not None:
            raw[name] = value.hex()
    boot_order = list(struct.unpack(f"<{len(order) // 2}H", order[: len(order) // 2 * 2]))
    for number in sorted(set(boot_order) | set(range(scan_range))):
        name = f"Boot{number:04X}"
        value = _get_variable(name, missing_ok=True)
        if value is not None:
            raw[name] = value.hex()
    return snapshot_from_raw(raw)
