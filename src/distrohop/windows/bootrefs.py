"""Reference graph: siapa merujuk ke siapa di ESP, NVRAM, dan partisi.

Prinsip:
- Pola nama hanya dipakai untuk *menemukan kandidat*, tidak pernah sebagai bukti.
- Setiap kesimpulan disertai alasan yang bisa diperiksa.
- Analisis ini tidak mencentang atau menghapus apa pun.

Tahap 1 (`collect_esp_refs`) membaca file kecil di ESP yang sedang ter-mount.
Tahap 2 (`analyze`) murni mengolah data, sehingga mudah dites.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from ..format import human_size
from . import gpt_types
from .bootentries import BootEntry
from .disks import DiskInventory, Partition
from .esp import EspFile, EspInventory, norm_path
from .fsprobe import FsInfo

VERDICT_LEFTOVER = "kemungkinan sisa"
VERDICT_IN_USE = "kemungkinan masih dipakai"
VERDICT_UNSURE = "tidak pasti"
VERDICT_PROTECTED = "dilindungi"
VERDICT_NOT_TARGET = "bukan target"

DISTRO_NAMES = {
    "fedora": "Fedora",
    "ubuntu": "Ubuntu",
    "debian": "Debian",
    "opensuse": "openSUSE",
    "arch": "Arch Linux",
    "manjaro": "Manjaro",
    "endeavouros": "EndeavourOS",
    "pop_os": "Pop!_OS",
    "pop": "Pop!_OS",
    "neon": "KDE neon",
    "kali": "Kali Linux",
    "centos": "CentOS",
    "rocky": "Rocky Linux",
    "almalinux": "AlmaLinux",
    "redhat": "Red Hat Enterprise Linux",
    "nixos": "NixOS",
    "gentoo": "Gentoo",
    "void": "Void Linux",
    "elementary": "elementary OS",
    "zorin": "Zorin OS",
    "mx": "MX Linux",
    "garuda": "Garuda Linux",
    "cachyos": "CachyOS",
    "nobara": "Nobara",
    "grub": "GRUB (generik)",
    "systemd": "systemd-boot",
    "refind": "rEFInd",
}
LINUX_LOADERS = {"shim", "grub", "systemd-boot", "refind", "mokmanager", "shim-fallback"}

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_FAT_UUID = re.compile(r"\b[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}\b")
_ROOT_ARG = re.compile(r"\broot=(UUID|PARTUUID)=([0-9A-Fa-f-]+)", re.IGNORECASE)
_BY_PATH = re.compile(r"/dev/disk/by-(uuid|partuuid)/([0-9A-Fa-f-]+)", re.IGNORECASE)
_LUKS_ARG = re.compile(r"\brd\.luks\.uuid=(?:luks-)?([0-9A-Fa-f-]+)", re.IGNORECASE)
_KERNEL_NAME = re.compile(r"(vmlinuz|initramfs|initrd|ucode|\.img$|^linux$)", re.IGNORECASE)
_TEXT_LIMIT = 1024 * 1024


# --- Tahap 1: kumpulkan rujukan dari ESP ------------------------------------


@dataclass(frozen=True)
class FsRef:
    kind: str  # "fs-uuid" (UUID filesystem/LUKS), "partuuid" (GUID partisi GPT), "partnum" (nomor partisi di disk ESP)
    value: str  # huruf kecil
    source: str  # dari mana rujukan ini dibaca


@dataclass
class EspFolder:
    path: str
    name: str
    size: int
    files: list[str]
    loaders: list[str]
    refs: list[FsRef]
    csv_entries: list[tuple[str, str]]
    notes: list[str] = field(default_factory=list)
    grub_prefixes: list[str] = field(default_factory=list)  # "EFI\Arch\grubx64.efi: (,gpt1)/grub"
    prefix_dirs: list[str] = field(default_factory=list)  # folder ESP yang ditunjuk prefix, mis. "grub"

    @property
    def base_name(self) -> str:
        """'Pop_OS-<uuid>' -> 'pop_os'."""
        return _UUID.sub("", self.name).strip("-_ ").lower()

    @property
    def display_name(self) -> str:
        return DISTRO_NAMES.get(self.base_name, self.name)

    @property
    def is_linux_candidate(self) -> bool:
        return bool(LINUX_LOADERS & set(self.loaders)) or bool(self.csv_entries) or self.base_name in DISTRO_NAMES


@dataclass
class LoaderEntry:
    path: str
    title: str | None
    linux: str | None
    initrd: list[str]
    efi: str | None
    options: str | None
    refs: list[FsRef]


@dataclass
class UkiImage:
    path: str
    os_id: str | None
    os_name: str | None
    refs: list[FsRef]


@dataclass
class FallbackLoader:
    path: str
    owner: str
    same_as_windows: bool
    size: int
    identical_to: list[str] = field(default_factory=list)  # file lain di ESP dengan SHA256 sama


@dataclass
class GrubDir:
    """Folder GRUB di luar EFI\\ (mis. 'grub\\' di root ESP, dibuat grub-install dengan --boot-directory)."""

    path: str
    size: int
    file_count: int
    refs: list[FsRef]
    core_prefixes: list[str]


@dataclass
class EspRefs:
    folders: list[EspFolder] = field(default_factory=list)
    grub_dirs: list[GrubDir] = field(default_factory=list)
    loader_entries: list[LoaderEntry] = field(default_factory=list)
    ukis: list[UkiImage] = field(default_factory=list)
    loose_files: list[str] = field(default_factory=list)
    loader_conf: str | None = None
    fallback: FallbackLoader | None = None


def loader_kind(filename: str) -> str | None:
    name = filename.lower()
    if not name.endswith(".efi"):
        return None
    for prefix, kind in (
        ("shim", "shim"),
        ("grub", "grub"),
        ("systemd-boot", "systemd-boot"),
        ("refind", "refind"),
        ("mm", "mokmanager"),
        ("fb", "shim-fallback"),
    ):
        if name.startswith(prefix):
            return kind
    return "efi"


def _dedupe(refs: list[FsRef]) -> list[FsRef]:
    seen: set[tuple[str, str]] = set()
    out = []
    for ref in refs:
        if (ref.kind, ref.value) not in seen:
            seen.add((ref.kind, ref.value))
            out.append(ref)
    return out


def refs_from_cmdline(text: str, source: str) -> list[FsRef]:
    refs = []
    for kind, value in _ROOT_ARG.findall(text):
        refs.append(FsRef("partuuid" if kind.upper() == "PARTUUID" else "fs-uuid", value.lower(), f"{source} (root=)"))
    for kind, value in _BY_PATH.findall(text):
        refs.append(FsRef("partuuid" if kind.lower() == "partuuid" else "fs-uuid", value.lower(), source))
    for value in _LUKS_ARG.findall(text):
        refs.append(FsRef("fs-uuid", value.lower(), f"{source} (rd.luks.uuid)"))
    return refs


def refs_from_grub_cfg(text: str, source: str) -> list[FsRef]:
    refs: list[FsRef] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("search"):
            for value in _UUID.findall(stripped):
                refs.append(FsRef("fs-uuid", value.lower(), f"{source} (search)"))
            for value in _FAT_UUID.findall(_UUID.sub(" ", stripped)):
                refs.append(FsRef("fs-uuid", value.lower(), f"{source} (search)"))
        refs.extend(refs_from_cmdline(stripped, source))
    return _dedupe(refs)


def parse_boot_csv(data: bytes) -> list[tuple[str, str]]:
    """BOOTX64.CSV milik shim: 'loader,label,options,keterangan' (biasanya UTF-16)."""
    if data.startswith(b"\xff\xfe"):
        text = data[2:].decode("utf-16-le", "replace")
    elif b"\x00" in data[:64]:
        text = data.decode("utf-16-le", "replace")
    else:
        text = data.decode("utf-8", "replace")
    entries = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            entries.append((parts[0], parts[1]))
    return entries


def parse_loader_entry(text: str, path: str) -> LoaderEntry:
    fields: dict[str, str] = {}
    initrd: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(" ")
        key, value = key.lower(), value.strip()
        if key == "initrd":
            initrd.append(value)
        elif key == "options" and "options" in fields:
            fields["options"] += " " + value
        else:
            fields.setdefault(key, value)
    options = fields.get("options")
    return LoaderEntry(
        path=path,
        title=fields.get("title"),
        linux=fields.get("linux"),
        initrd=initrd,
        efi=fields.get("efi"),
        options=options,
        refs=_dedupe(refs_from_cmdline(options or "", path)),
    )


def parse_os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key] = value.strip().strip("\"'")
    return values


def read_pe_sections(path: Path, names: Sequence[str] = (".osrel", ".cmdline")) -> dict[str, bytes]:
    """Baca section tertentu dari file PE (Unified Kernel Image) tanpa memuat seluruh file."""
    sections: dict[str, bytes] = {}
    with open(path, "rb") as handle:
        head = handle.read(64)
        if len(head) < 64 or head[:2] != b"MZ":
            return sections
        pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
        handle.seek(pe_offset)
        header = handle.read(24)
        if len(header) < 24 or header[:4] != b"PE\x00\x00":
            return sections
        count = min(struct.unpack_from("<H", header, 6)[0], 96)
        optional_size = struct.unpack_from("<H", header, 20)[0]
        handle.seek(pe_offset + 24 + optional_size)
        table = handle.read(40 * count)
        for i in range(len(table) // 40):
            name = table[i * 40 : i * 40 + 8].rstrip(b"\x00").decode("ascii", "replace")
            virtual_size, _, raw_size, raw_pointer = struct.unpack_from("<IIII", table, i * 40 + 8)
            if name in names:
                handle.seek(raw_pointer)
                sections[name] = handle.read(min(virtual_size or raw_size, raw_size, _TEXT_LIMIT))
    return sections


_FALLBACK_SIGNATURES = (
    (b"systemd-boot", "systemd-boot"),
    (b"rEFInd", "rEFInd"),
    (b"UEFI SHIM", "shim"),
    (b"shim", "shim"),
    (b"GNU GRUB", "GRUB"),
    (b"grub", "GRUB"),
    (b"Microsoft Corporation", "Microsoft (berbeda dari bootmgfw.efi)"),
)


def fallback_owner(data: bytes) -> str:
    for signature, owner in _FALLBACK_SIGNATURES:
        if signature in data:
            return owner
    return "tidak dikenal"


_GRUB_OBJ_CONFIG = 2
_GRUB_OBJ_PREFIX = 3
_PREFIX_DEVICE = re.compile(r"^\((?P<device>[^)]*)\)(?P<path>.*)$")


def _parse_module_info(data: bytes, start: int) -> tuple[str | None, str | None] | None:
    for layout, header_size in (("<IIQQ", 24), ("<III", 12)):  # grub_module_info64 / grub_module_info32
        if start + header_size > len(data):
            continue
        values = struct.unpack_from(layout, data, start)
        offset, total = (values[2], values[3]) if header_size == 24 else (values[1], values[2])
        if offset != header_size or total < header_size or start + total > len(data):
            continue
        prefix = config = None
        position, end = start + offset, start + total
        while position + 8 <= end:
            kind, size = struct.unpack_from("<II", data, position)
            if size < 8 or position + size > end:
                break
            payload = data[position + 8 : position + size].split(b"\x00", 1)[0].decode("utf-8", "replace")
            if kind == _GRUB_OBJ_PREFIX:
                prefix = payload
            elif kind == _GRUB_OBJ_CONFIG:
                config = payload
            position += size
        else:
            return prefix, config
    return None


def grub_embedded(data: bytes) -> tuple[str | None, str | None]:
    """(prefix, config tertanam) dari core image GRUB, dibaca dari blok modul 'mimg' yang dibuat grub-mkimage."""
    start = data.find(b"mimg")
    while start != -1:
        parsed = _parse_module_info(data, start)
        if parsed is not None:
            return parsed
        start = data.find(b"mimg", start + 4)
    return None, None


def _prefix_refs(prefix: str, source: str, esp_number: int | None) -> list[FsRef]:
    """Prefix yang menunjuk partisi selain ESP jadi rujukan nomor partisi (dicek di tahap analisis)."""
    _, number = split_grub_prefix(prefix)
    if number is not None and number != esp_number:
        return [FsRef("partnum", str(number), f"prefix GRUB '{prefix}' di {source}")]
    return []


def split_grub_prefix(prefix: str) -> tuple[str | None, int | None]:
    """'(,gpt1)/grub' -> ('grub', 1). Nomor partisi None kalau prefix tidak menyebut partisi."""
    match = _PREFIX_DEVICE.match(prefix.strip())
    device, path = (match["device"], match["path"]) if match else ("", prefix.strip())
    gpt = re.search(r"gpt(\d+)", device)
    return norm_path(path) or None, int(gpt.group(1)) if gpt else None


def _read(root: Path, rel: str, limit: int = _TEXT_LIMIT) -> bytes | None:
    try:
        with open(root.joinpath(*rel.split("\\")), "rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


def collect_esp_refs(root: Path, esp: EspInventory) -> EspRefs:
    refs = EspRefs()

    for directory in esp.dirs:
        parts = directory.split("\\")
        if len(parts) != 2 or parts[0].lower() != "efi" or parts[1].lower() in ("microsoft", "boot", "linux"):
            continue
        files = esp.under(directory)
        loaders = sorted({k for f in files if (k := loader_kind(f.name)) and k != "efi"})
        folder_refs: list[FsRef] = []
        csv_entries: list[tuple[str, str]] = []
        notes: list[str] = []
        for f in files:
            lowered = f.name.lower()
            if lowered == "grub.cfg":
                text = (_read(root, f.path) or b"").decode("utf-8", "replace")
                folder_refs.extend(refs_from_grub_cfg(text, f.path))
                if "menuentry" in text:
                    notes.append(f"{f.path} adalah grub.cfg lengkap (bukan stub)")
            elif lowered.startswith("boot") and lowered.endswith(".csv"):
                csv_entries.extend(parse_boot_csv(_read(root, f.path) or b""))

        grub_prefixes: list[str] = []
        prefix_dirs: list[str] = []
        for f in files:
            if loader_kind(f.name) != "grub":
                continue
            prefix, config = grub_embedded(_read(root, f.path, limit=16 * 1024 * 1024) or b"")
            if config:
                folder_refs.extend(refs_from_grub_cfg(config, f"{f.path} (config tertanam)"))
            if not prefix:
                continue
            grub_prefixes.append(f"{f.path}: {prefix}")
            target, partition_number = split_grub_prefix(prefix)
            if partition_number is not None and partition_number != esp.partition_number:
                folder_refs.extend(_prefix_refs(prefix, f.path, esp.partition_number))
                continue
            actual = esp.find_dir(target) if target else None
            if actual is None:
                notes.append(f"Prefix GRUB '{prefix}' di {f.path} menunjuk folder yang tidak ada di ESP")
            elif actual.lower() != directory.lower() and actual not in prefix_dirs:
                prefix_dirs.append(actual)
                cfg = esp.find(actual + "\\grub.cfg")
                if cfg is not None:
                    text = (_read(root, cfg.path) or b"").decode("utf-8", "replace")
                    folder_refs.extend(refs_from_grub_cfg(text, cfg.path))

        for value in _UUID.findall(parts[1]):
            folder_refs.append(FsRef("fs-uuid", value.lower(), f"nama folder {directory}"))
        refs.folders.append(
            EspFolder(
                path=directory,
                name=parts[1],
                size=sum(f.size for f in files),
                files=[f.path for f in files],
                loaders=loaders,
                refs=_dedupe(folder_refs),
                csv_entries=csv_entries,
                notes=notes,
                grub_prefixes=grub_prefixes,
                prefix_dirs=prefix_dirs,
            )
        )

    for directory in esp.dirs:
        if "\\" in directory or directory.lower() in ("efi", "loader"):
            continue
        files = esp.under(directory)
        config = esp.find(directory + "\\grub.cfg")
        if config is None and not any(f.name.lower().endswith(".mod") for f in files):
            continue
        grub_refs: list[FsRef] = []
        if config is not None:
            grub_refs.extend(refs_from_grub_cfg((_read(root, config.path) or b"").decode("utf-8", "replace"), config.path))
        core_prefixes = []
        for f in files:
            if f.name.lower() == "core.efi" or loader_kind(f.name) == "grub":
                prefix, embedded = grub_embedded(_read(root, f.path, limit=16 * 1024 * 1024) or b"")
                if prefix:
                    core_prefixes.append(f"{f.path}: {prefix}")
                    grub_refs.extend(_prefix_refs(prefix, f.path, esp.partition_number))
                if embedded:
                    grub_refs.extend(refs_from_grub_cfg(embedded, f"{f.path} (config tertanam)"))
        refs.grub_dirs.append(
            GrubDir(directory, sum(f.size for f in files), len(files), _dedupe(grub_refs), sorted(set(core_prefixes)))
        )

    for f in esp.files:
        lowered = f.path.lower()
        if lowered.startswith("loader\\entries\\") and lowered.endswith(".conf"):
            text = (_read(root, f.path) or b"").decode("utf-8", "replace")
            refs.loader_entries.append(parse_loader_entry(text, f.path))
        elif lowered == "loader\\loader.conf":
            refs.loader_conf = f.path
        elif lowered.startswith("efi\\linux\\") and lowered.endswith(".efi"):
            try:
                sections = read_pe_sections(root.joinpath(*f.path.split("\\")))
            except OSError:
                sections = {}
            osrel = parse_os_release(sections.get(".osrel", b"").decode("utf-8", "replace"))
            cmdline = sections.get(".cmdline", b"").decode("utf-8", "replace")
            refs.ukis.append(
                UkiImage(
                    path=f.path,
                    os_id=osrel.get("ID"),
                    os_name=osrel.get("PRETTY_NAME") or osrel.get("NAME"),
                    refs=_dedupe(refs_from_cmdline(cmdline, f.path)),
                )
            )
        elif lowered.split("\\", 1)[0] not in ("efi", "loader"):
            refs.loose_files.append(f.path)

    fallback = esp.find("EFI\\Boot\\bootx64.efi")
    if fallback is not None:
        windows = esp.find("EFI\\Microsoft\\Boot\\bootmgfw.efi")
        same = bool(windows and windows.sha256 == fallback.sha256)
        identical = [
            f.path
            for f in esp.files
            if f.sha256 == fallback.sha256 and not f.path.lower().startswith("efi\\boot\\")
        ]
        if same:
            owner = "Microsoft (sama persis dengan bootmgfw.efi)"
        else:
            owner = fallback_owner(_read(root, fallback.path, limit=8 * 1024 * 1024) or b"")
            if identical:
                owner += f" (identik dengan {identical[0]})"
        refs.fallback = FallbackLoader(fallback.path, owner, same, fallback.size, identical)
    return refs


# --- Tahap 2: analisis ------------------------------------------------------


@dataclass
class Component:
    kind: str  # "boot-entry" | "esp-folder" | "esp-file" | "partition"
    ref: str  # id stabil: "nv:0003", "EFI\\fedora", "disk0-p5"
    label: str
    size: int | None = None
    evidence: list[str] = field(default_factory=list)
    protected: bool = False


@dataclass
class Installation:
    key: str
    name: str
    kind: str  # "linux" | "windows" | "firmware" | "unattached" | "other"
    verdict: str
    reasons: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)


@dataclass
class Analysis:
    installations: list[Installation]
    boot_order: list[BootEntry]
    fallback: FallbackLoader | None
    notes: list[str]


@dataclass
class _Group:
    key: str
    name: str
    kind: str
    components: list[Component] = field(default_factory=list)
    refs: list[FsRef] = field(default_factory=list)
    loaders: set[str] = field(default_factory=set)
    entries: list[tuple[BootEntry, str]] = field(default_factory=list)  # (entri, status file)
    has_folder: bool = False
    notes: list[str] = field(default_factory=list)


class _Context:
    def __init__(self, inventory, fsinfo, fs_probe_complete, esp):
        self.inventory: DiskInventory = inventory or DiskInventory()
        self.fsinfo: Mapping[str, FsInfo] = fsinfo
        self.fs_probe_complete = fs_probe_complete
        self.esp: EspInventory | None = esp
        self.fallback: FallbackLoader | None = None
        self.by_fs_uuid = {
            info.uuid.lower(): part
            for part in self.inventory.partitions
            if (info := fsinfo.get(part.id)) and info.uuid
        }
        # Partisi yang UUID filesystem di dalamnya tidak terlihat: belum terbaca, tidak dikenali,
        # atau wadah (LUKS/LVM) yang menyembunyikan filesystem aslinya.
        self.opaque = [
            part
            for part in self.inventory.partitions
            if part.category in (gpt_types.LINUX, "unknown", "other")
            and (
                (info := fsinfo.get(part.id)) is None
                or not info.uuid
                or info.fs_type in ("crypto_LUKS", "LVM2_member")
            )
        ]
        self.uuid_index_complete = fs_probe_complete and not self.opaque

    def resolve(self, ref: FsRef) -> tuple[Partition | None, str]:
        """(partisi, status) dengan status 'ada', 'tidak ada', atau 'tidak diketahui'.

        'tidak ada' hanya dipakai kalau semua partisi Linux terbaca jelas; selain itu UUID-nya
        mungkin tersembunyi di partisi yang tidak terbaca, jadi statusnya 'tidak diketahui'.
        """
        if ref.kind == "partuuid":
            part = self.inventory.by_guid(ref.value)
            return part, "ada" if part else "tidak ada"
        if ref.kind == "partnum":
            part = self.partition_by_number(int(ref.value))
            if part is not None and part.category in (gpt_types.LINUX, "unknown"):
                return part, "ada"
            return None, "tidak ada"
        part = self.by_fs_uuid.get(ref.value)
        if part:
            return part, "ada"
        return None, "tidak ada" if self.uuid_index_complete else "tidak diketahui"

    def partition_by_number(self, number: int) -> Partition | None:
        """Partisi bernomor `number` di disk tempat ESP berada (GRUB '(,gptN)' = disk yang sama)."""
        esp_part = self.inventory.by_guid(self.esp.partition_guid) if self.esp else None
        if esp_part is None:
            return None
        return next(
            (p for p in self.inventory.partitions if p.disk_number == esp_part.disk_number and p.number == number),
            None,
        )

    def missing_text(self, ref: FsRef) -> str:
        if ref.kind == "partnum":
            source = ref.source[:1].upper() + ref.source[1:]
            part = self.partition_by_number(int(ref.value))
            if part is None:
                return f"{source} menunjuk partisi nomor {ref.value} di disk ini, yang sekarang tidak ada"
            filesystem = f" ({part.filesystem})" if part.filesystem else ""
            return (
                f"{source} menunjuk partisi nomor {ref.value} di disk ini, yang sekarang adalah "
                f"{part.type_name}{filesystem}, bukan partisi Linux (dengan asumsi penomoran GRUB sama dengan Windows)"
            )
        return f"Merujuk {ref.kind} {ref.value} (dari {ref.source}) yang sudah tidak ada di disk mana pun"

    def role(self, part: Partition) -> str:
        """Peran partisi Linux: root, boot, home, swap, data, mungkin-root, atau tidak-diketahui."""
        info = self.fsinfo.get(part.id)
        if part.gpt_type == gpt_types.LINUX_ROOT_X86_64:
            return "root"
        if part.gpt_type == gpt_types.LINUX_HOME:
            return "home"
        if part.category == "esp":
            return "boot"
        if info is None:
            return "swap" if part.gpt_type == gpt_types.LINUX_SWAP else "tidak-diketahui"
        if info.fs_type == "swap":
            return "swap"
        mounted = info.last_mounted
        if mounted == "/":
            return "root"
        if mounted in ("/boot", "/boot/efi", "/efi") or part.gpt_type == gpt_types.XBOOTLDR:
            return "boot"
        if mounted and mounted.startswith("/home"):
            return "home"
        if mounted:
            return "data"
        return "mungkin-root"

    def partition_evidence(self, part: Partition) -> list[str]:
        info = self.fsinfo.get(part.id)
        evidence = [
            f"{human_size(part.size)} · offset {part.offset:,} byte · tipe GPT: {part.type_name}",
            f"GUID partisi: {part.guid}",
        ]
        if info:
            evidence.append(f"Isi: {info.describe()}")
        elif self.fs_probe_complete:
            evidence.append("Isi: filesystem tidak dikenali")
        else:
            evidence.append("Isi: belum bisa dibaca (butuh Administrator)")
        if part.mount_points or part.drive_letter:
            evidence.append("Punya drive letter / mount point")
        return evidence


def _boot_entry_component(entry: BootEntry, ctx: _Context) -> tuple[Component, str]:
    evidence = [f"Terlihat di: {' + '.join(entry.sources)}"]
    evidence += [f"Tidak cocok: {c}" for c in entry.conflicts]
    evidence.append(f"Urutan boot ke-{entry.order + 1}" if entry.order is not None else "Tidak ada di urutan boot")
    if entry.active is False:
        evidence.append("Status: tidak aktif")
    if entry.path:
        evidence.append(f"Menjalankan: {entry.path}")

    esp_guid = ctx.esp.partition_guid if ctx.esp else None
    on_esp = entry.partition_guid is None or entry.partition_guid == esp_guid
    if entry.partition_guid:
        target = ctx.inventory.by_guid(entry.partition_guid)
        if target is None:
            evidence.append(
                f"Merujuk partisi {entry.partition_guid} yang tidak ada di disk mana pun "
                "(USB yang dicabut, atau partisi yang sudah dihapus)"
            )
        elif target.guid != esp_guid:
            evidence.append(f"File-nya berada di partisi {target.id}, bukan di ESP")

    if not entry.path and entry.partition_guid and entry.partition_guid == esp_guid and ctx.esp is not None:
        if ctx.fallback is not None:
            evidence.append(
                f"Tanpa path file: firmware menjalankan file fallback {ctx.fallback.path} di ESP "
                f"(saat ini: {ctx.fallback.owner})"
            )
        else:
            evidence.append("Tanpa path file: firmware mencari file fallback EFI\\Boot\\bootx64.efi, yang saat ini tidak ada")

    status = "tidak dicek"
    if entry.path and ctx.esp and on_esp:
        if ctx.esp.find(entry.path):
            status = "ada"
            evidence.append("File .efi-nya ada di ESP")
        else:
            status = "tidak ada"
            evidence.append("File .efi-nya sudah TIDAK ADA di ESP (entri yatim)")
    elif entry.partition_guid and ctx.inventory.by_guid(entry.partition_guid) is None:
        status = "tidak ada"

    label = f"Entri boot '{entry.description}' ({entry.label})"
    return Component("boot-entry", entry.key, label, evidence=evidence), status


def _verdict(
    group: _Group, ctx: _Context, linux_parts: list[Partition], attached: Mapping[str, str]
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    resolved = [(ref, *ctx.resolve(ref)) for ref in group.refs]
    found: dict[str, Partition] = {}
    for ref, part, status in resolved:
        if part is None:
            continue
        if part.category == "esp":
            reasons.append(f"Merujuk ESP itu sendiri (dari {ref.source})")
            continue
        found.setdefault(part.id, part)
    missing = [ref for ref, part, status in resolved if status == "tidak ada"]
    unknown = [ref for ref, part, status in resolved if status == "tidak diketahui"]
    roles = {pid: ctx.role(part) for pid, part in found.items()}
    orphans = [e for e, status in group.entries if status == "tidak ada"]

    for ref in missing:
        reasons.append(ctx.missing_text(ref))
    for pid, role in roles.items():
        reasons.append(f"Merujuk partisi {pid} (peran: {role})")
    for entry in orphans:
        reasons.append(f"Entri '{entry.description}' menjalankan file yang sudah tidak ada")

    if any(role == "root" for role in roles.values()):
        root = next(pid for pid, role in roles.items() if role == "root")
        verdict = VERDICT_IN_USE
        reasons.append(f"Partisi {root} terakhir di-mount sebagai / (root), jadi instalasi ini kemungkinan masih utuh")
    elif any(role in ("mungkin-root", "tidak-diketahui") for role in roles.values()):
        verdict = VERDICT_UNSURE
        reasons.append("Ada partisi yang dirujuk dan mungkin berisi root, tapi tidak bisa dipastikan dari Windows")
    elif unknown:
        verdict = VERDICT_UNSURE
        hidden = ", ".join(p.id for p in ctx.opaque) or "-"
        reasons.append(
            f"Sebagian rujukan tidak bisa dicek: filesystem yang dirujuk mungkin ada di partisi yang isinya "
            f"tidak terbaca/terenkripsi ({hidden})"
        )
    elif found:
        # Calon root: partisi Linux yang belum jadi milik instalasi lain (root distro lain tidak dihitung).
        candidates = [
            p
            for p in linux_parts
            if p.id not in found
            and attached.get(p.id, group.key) == group.key
            and ctx.role(p) in ("root", "mungkin-root", "tidak-diketahui")
        ]
        if candidates:
            verdict = VERDICT_UNSURE
            ids = ", ".join(p.id for p in candidates)
            reasons.append(
                f"Partisi yang dirujuk hanya berperan boot/swap/data, tapi ada partisi Linux lain ({ids}) "
                "yang mungkin root-nya; hubungannya tidak bisa dipastikan dari Windows"
            )
            hint = group.name.split()[0].lower()
            for part in candidates:
                info = ctx.fsinfo.get(part.id)
                if info and info.label and hint and hint in info.label.lower():
                    reasons.append(
                        f"Label filesystem {part.id} ('{info.label}') mirip nama instalasi ini, "
                        "jadi kemungkinan besar itu root-nya"
                    )
        else:
            verdict = VERDICT_LEFTOVER
            reasons.append("Hanya partisi pendukung (mis. /boot) yang tersisa; tidak ada partisi root Linux di disk")
    elif missing:
        verdict = VERDICT_LEFTOVER
        reasons.append("Semua yang dirujuk (filesystem/partisi) sudah tidak ada")
    elif orphans and not group.has_folder:
        verdict = VERDICT_LEFTOVER
    else:
        # Tanpa rujukan: lihat apakah masih ada partisi yang bisa jadi root instalasi ini.
        free = [
            p
            for p in ctx.inventory.partitions
            if p.category in (gpt_types.LINUX, "unknown") and attached.get(p.id, group.key) == group.key
        ]
        if free:
            verdict = VERDICT_UNSURE
            ids = ", ".join(p.id for p in free)
            reasons.append(f"Tidak ada rujukan yang bisa dibaca, dan masih ada partisi ({ids}) yang belum terhubung ke instalasi mana pun")
        elif ctx.fs_probe_complete:
            verdict = VERDICT_LEFTOVER
            owned_elsewhere = any(owner != group.key for owner in attached.values())
            detail = " (semua partisi Linux yang ada sudah terhubung ke instalasi lain)" if owned_elsewhere else ""
            reasons.append(
                f"Tidak ada rujukan yang bisa dibaca, dan tidak ada partisi yang bisa jadi root-nya{detail}. "
                "Kalau distro ini terpasang di disk eksternal, colok disk itu lalu scan ulang"
            )
        else:
            verdict = VERDICT_UNSURE
            reasons.append("Tidak ada rujukan yang bisa dibaca, dan isi partisi belum terbaca (butuh Administrator)")

    for entry, _ in group.entries:
        if entry.order == 0:
            reasons.append(
                f"Entri '{entry.description}' ada di urutan boot PERTAMA, karena itu bootloader ini "
                "yang muncul setiap kali laptop dinyalakan"
            )
    return verdict, reasons + group.notes


def _ref_text(ref: FsRef) -> str:
    if ref.kind == "partnum":
        return f"Menunjuk partisi nomor {ref.value} (dari {ref.source})"
    return f"Merujuk {ref.kind} {ref.value} (dari {ref.source})"


def _sd_key(refs: list[FsRef], fallback: str | None) -> str:
    root_ref = next((r for r in refs if "root=" in r.source), refs[0] if refs else None)
    if root_ref:
        return f"sd:{root_ref.value}"
    return f"sd:{(fallback or 'systemd-boot').lower()}"


def _sd_name(title: str | None) -> str:
    if not title:
        return "systemd-boot"
    return title.split("(", 1)[0].strip() or title


def analyze(
    inventory: DiskInventory | None,
    fsinfo: Mapping[str, FsInfo],
    fs_probe_complete: bool,
    boot_entries: Sequence[BootEntry],
    esp: EspInventory | None,
    esp_refs: EspRefs | None,
    extra_notes: Sequence[str] = (),
) -> Analysis:
    ctx = _Context(inventory, fsinfo, fs_probe_complete, esp)
    ctx.fallback = esp_refs.fallback if esp_refs else None
    groups: dict[str, _Group] = {}

    def group(key: str, name: str, kind: str) -> _Group:
        return groups.setdefault(key, _Group(key, name, kind))

    windows = group("windows", "Windows", "windows")
    folder_groups: dict[str, str] = {}  # nama folder (huruf kecil) -> key grup
    file_owner: dict[str, str] = {}  # path file ESP (huruf kecil) -> key grup pemiliknya
    used_files: set[str] = set()

    if esp_refs is not None and esp is not None:
        for folder in esp_refs.folders:
            evidence = [f"{len(folder.files)} file, {human_size(folder.size)}"]
            if folder.loaders:
                evidence.append(f"Bootloader terdeteksi: {', '.join(folder.loaders)}")
            for ref in folder.refs:
                evidence.append(_ref_text(ref))
            for loader, label in folder.csv_entries:
                evidence.append(
                    f"BOOT CSV: shim fallback akan membuat ulang entri '{label}' ({loader}) selama folder ini ada"
                )
            for prefix in folder.grub_prefixes:
                evidence.append(f"Prefix GRUB yang tertanam di {prefix}")
            evidence.extend(folder.notes)
            component = Component("esp-folder", folder.path, f"Folder {folder.path}", folder.size, evidence)
            if folder.is_linux_candidate:
                target = group(f"efi:{folder.name.lower()}", folder.display_name, "linux")
                target.has_folder = True
                target.refs.extend(folder.refs)
                target.loaders.update(folder.loaders)
            else:
                target = group("other-esp", "Folder lain di ESP (bukan bootloader Linux yang dikenal)", "other")
                component.evidence.append("Tidak berisi bootloader Linux yang dikenal; bisa jadi tool pabrikan laptop")
            target.components.append(component)
            folder_groups[folder.name.lower()] = target.key
            for path in folder.files:
                file_owner[path.lower()] = target.key
            fallback = esp_refs.fallback
            if fallback and folder.is_linux_candidate:
                twins = [p for p in fallback.identical_to if p.lower().startswith(folder.path.lower() + "\\")]
                if twins:
                    target.notes.append(
                        f"File fallback firmware {fallback.path} identik dengan {twins[0]}, jadi fallback itu "
                        "dipasang oleh instalasi ini. Kalau instalasi ini dibersihkan, fallback perlu dikembalikan "
                        "ke milik Windows supaya laptop tetap bisa boot kalau NVRAM ter-reset"
                    )

        for entry in esp_refs.loader_entries:
            target = group(_sd_key(entry.refs, entry.title), _sd_name(entry.title), "linux")
            target.loaders.add("systemd-boot")
            target.refs.extend(entry.refs)
            evidence = [f"Judul: {entry.title or '-'}"]
            evidence += [f"Merujuk {r.kind} {r.value}" for r in entry.refs]
            esp_file = esp.find(entry.path)
            target.components.append(
                Component("esp-file", entry.path, f"Entri systemd-boot {entry.path}", esp_file.size if esp_file else None, evidence)
            )
            used_files.add(entry.path.lower())
            for referenced in [entry.linux, entry.efi, *entry.initrd]:
                found = esp.find(norm_path(referenced)) if referenced else None
                if found and found.path.lower() not in used_files:
                    used_files.add(found.path.lower())
                    file_owner[found.path.lower()] = target.key
                    target.components.append(
                        Component("esp-file", found.path, f"File {found.path}", found.size, [f"Dirujuk oleh {entry.path}"])
                    )
                elif referenced and not found:
                    target.notes.append(f"{entry.path} merujuk {referenced} yang tidak ada di ESP (mungkin di XBOOTLDR)")

        for uki in esp_refs.ukis:
            target = group(_sd_key(uki.refs, uki.os_id), uki.os_name or uki.os_id or "UKI", "linux")
            target.loaders.add("uki")
            target.refs.extend(uki.refs)
            evidence = [f"Distro (dari .osrel): {uki.os_name or uki.os_id or 'tidak diketahui'}"]
            evidence += [f"Merujuk {r.kind} {r.value} (dari .cmdline)" for r in uki.refs]
            esp_file = esp.find(uki.path)
            target.components.append(
                Component("esp-file", uki.path, f"Unified Kernel Image {uki.path}", esp_file.size if esp_file else None, evidence)
            )
            file_owner[uki.path.lower()] = target.key

        sd_groups = [g for g in groups.values() if g.key.startswith("sd:")]
        systemd = groups.get("efi:systemd")
        if systemd and len(sd_groups) == 1:
            sd_groups[0].components.extend(systemd.components)
            sd_groups[0].has_folder = True
            folder_groups["systemd"] = sd_groups[0].key
            for path, owner in list(file_owner.items()):
                if owner == "efi:systemd":
                    file_owner[path] = sd_groups[0].key
            del groups["efi:systemd"]
        elif systemd and len(sd_groups) > 1:
            systemd.notes.append("systemd-boot dipakai bersama oleh lebih dari satu instalasi")
        if esp_refs.loader_conf:
            owner = sd_groups[0] if len(sd_groups) == 1 else groups.get("efi:systemd") or group(
                "unattached", "Tidak terkait instalasi mana pun", "unattached"
            )
            conf = esp.find(esp_refs.loader_conf)
            owner.components.append(
                Component("esp-file", esp_refs.loader_conf, f"Konfigurasi {esp_refs.loader_conf}", conf.size if conf else None)
            )

        # Folder prefix GRUB (mis. "grub\" di root ESP) -> grup pemilik core image yang menunjuknya.
        prefix_owner: dict[str, tuple[str, str]] = {}
        for folder in esp_refs.folders:
            key = folder_groups.get(folder.name.lower())
            for directory in folder.prefix_dirs:
                prefix_owner[directory.lower()] = (key, f"Folder ini adalah prefix GRUB yang tertanam di {folder.path}")

        grub_dirs = {g.path.lower(): g for g in esp_refs.grub_dirs}
        loose = [p for p in esp_refs.loose_files if p.lower() not in used_files]
        by_top: dict[str, list[str]] = {}
        for path in loose:
            top = path.split("\\", 1)[0] if "\\" in path else ""
            by_top.setdefault(top, []).append(path)
        for top, paths in by_top.items():
            if not top:  # file langsung di root ESP
                for path in paths:
                    found = esp.find(path)
                    hint = (
                        "Nama mirip file kernel/initramfs Linux"
                        if _KERNEL_NAME.search(path)
                        else "File di luar folder EFI; asal-usulnya tidak diketahui"
                    )
                    group("unattached", "Tidak terkait instalasi mana pun", "unattached").components.append(
                        Component("esp-file", path, f"File {path}", found.size if found else None, [hint])
                    )
                continue
            size = sum(f.size for p in paths if (f := esp.find(p)))
            evidence = [f"{len(paths)} file, {human_size(size)}"]
            component = Component("esp-folder", top, f"Folder {top}\\", size, evidence)
            owner = prefix_owner.get(top.lower())
            grub_dir = grub_dirs.get(top.lower())
            if owner and owner[0] in groups:
                component.evidence.append(owner[1])
                groups[owner[0]].components.append(component)
            elif grub_dir is not None:
                # Folder GRUB yang tidak ditunjuk bootloader mana pun: nilai dari isinya sendiri.
                target = group(f"grubdir:{top.lower()}", f"GRUB tanpa pemilik ({top}\\)", "linux")
                target.has_folder = True
                target.loaders.add("grub")
                target.refs.extend(grub_dir.refs)
                component.evidence.append(
                    "Berisi konfigurasi/modul GRUB, tapi tidak ada bootloader di EFI\\ yang menunjuk folder ini"
                )
                component.evidence += [f"Prefix tertanam di {p}" for p in grub_dir.core_prefixes]
                component.evidence += [_ref_text(r) for r in grub_dir.refs]
                target.components.append(component)
            else:
                component.evidence.append("Folder di luar EFI\\ yang tidak dirujuk bootloader mana pun; asal-usulnya tidak diketahui")
                group("unattached", "Tidak terkait instalasi mana pun", "unattached").components.append(component)

        windows_dir = next((d for d in esp.dirs if d.lower() == "efi\\microsoft"), None)
        if windows_dir:
            windows.components.append(
                Component(
                    "esp-folder",
                    windows_dir,
                    f"Folder {windows_dir}",
                    esp.size_of(windows_dir),
                    ["Bootloader Windows. Selalu dikunci."],
                    protected=True,
                )
            )

    for entry in boot_entries:
        component, status = _boot_entry_component(entry, ctx)
        path = (entry.path or "").lower().lstrip("\\").split("\\")
        owner = file_owner.get(norm_path(entry.path).lower()) if entry.path else None
        if entry.is_windows_boot_manager or path[:2] == ["efi", "microsoft"]:
            target = windows
            component.protected = True
        elif owner is not None and owner in groups:
            target = groups[owner]
        elif not entry.path:
            target = group("firmware", "Entri bawaan firmware / perangkat", "firmware")
        elif path[:2] == ["efi", "boot"]:
            target = group("firmware", "Entri bawaan firmware / perangkat", "firmware")
            component.evidence.append("Menjalankan file fallback EFI\\Boot (entri generik buatan firmware)")
        elif len(path) >= 3 and path[0] == "efi" and path[1] != "linux":
            key = folder_groups.get(path[1])
            if key is None:
                name = DISTRO_NAMES.get(_UUID.sub("", path[1]).strip("-_ "), path[1])
                key = group(f"efi:{path[1]}", name, "linux").key
                folder_groups[path[1]] = key
            target = groups[key]
        else:
            target = group("unattached", "Tidak terkait instalasi mana pun", "unattached")
        target.components.insert(sum(1 for c in target.components if c.kind == "boot-entry"), component)
        target.entries.append((entry, status))

    linux_parts = [p for p in ctx.inventory.partitions if p.category == gpt_types.LINUX]
    attached: dict[str, str] = {}
    for g in [g for g in groups.values() if g.kind == "linux"]:
        for ref in g.refs:
            part, _ = ctx.resolve(ref)
            if part is None or part.category == "esp":
                continue
            if part.id in attached:
                if attached[part.id] != g.key:
                    groups[attached[part.id]].notes.append(f"Partisi {part.id} juga dirujuk oleh {g.name}")
                continue
            attached[part.id] = g.key
            g.components.append(
                Component("partition", part.id, f"Partisi {part.id} · {human_size(part.size)} · {part.type_name}",
                          part.size, ctx.partition_evidence(part) + [f"Dirujuk oleh {ref.source}"])
            )

    leftovers = [p for p in ctx.inventory.partitions if p.category in (gpt_types.LINUX, "unknown", "other")]
    for part in leftovers:
        if part.id in attached:
            continue
        unattached = group("unattached", "Tidak terkait instalasi mana pun", "unattached")
        evidence = ctx.partition_evidence(part)
        if part.category == gpt_types.LINUX:
            evidence.append(f"Peran yang terdeteksi: {ctx.role(part)}")
        else:
            evidence.append("Bukan tipe partisi Linux; tidak bisa dipilih untuk dihapus di v1")
        unattached.components.append(
            Component("partition", part.id, f"Partisi {part.id} · {human_size(part.size)} · {part.type_name}", part.size, evidence)
        )

    for part in ctx.inventory.partitions:
        if part.category in gpt_types.PROTECTED_CATEGORIES:
            where = f" ({part.drive_letter}:)" if part.drive_letter else ""
            windows.components.append(
                Component(
                    "partition",
                    part.id,
                    f"Partisi {part.id}{where} · {human_size(part.size)} · {part.type_name}",
                    part.size,
                    ["Dilindungi, tidak bisa dipilih"],
                    protected=True,
                )
            )

    installations: list[Installation] = []
    for g in groups.values():
        if g.kind == "linux":
            verdict, reasons = _verdict(g, ctx, linux_parts, attached)
        elif g.kind == "windows":
            verdict, reasons = VERDICT_PROTECTED, _windows_health(g, ctx, boot_entries)
        elif g.kind == "firmware":
            verdict = VERDICT_NOT_TARGET
            reasons = ["Entri bawaan firmware atau perangkat (jaringan, USB, fallback). Tidak disentuh DistroHop."]
        elif g.kind == "other":
            verdict = VERDICT_NOT_TARGET
            reasons = ["Folder ini tidak berisi bootloader Linux yang dikenal. Jangan dihapus kecuali kamu tahu isinya."]
        else:
            verdict = VERDICT_UNSURE
            reasons = ["Tidak terhubung ke instalasi mana pun. Periksa detailnya sebelum memutuskan apa pun."]
            reasons += g.notes
        if g.components or g.kind == "windows":
            installations.append(Installation(g.key, g.name, g.kind, verdict, reasons, g.components))

    kind_order = {"linux": 0, "unattached": 1, "other": 2, "windows": 3, "firmware": 4}
    installations.sort(key=lambda i: (kind_order.get(i.kind, 9), i.name.lower()))

    notes = list(extra_notes)
    if esp is None:
        notes.append("ESP tidak bisa dibaca, jadi folder bootloader belum dianalisis (butuh Administrator).")
    elif esp.unreadable:
        listed = ", ".join(esp.unreadable[:3]) + (" …" if len(esp.unreadable) > 3 else "")
        notes.append(
            f"{len(esp.unreadable)} file di ESP tidak bisa dibaca karena sedang dipakai Windows ({listed}). "
            "Ini normal untuk BCD; di M2 BCD dicadangkan lewat bcdedit /export, bukan disalin langsung."
        )
    if not fs_probe_complete:
        notes.append("Isi sebagian partisi belum terbaca, jadi rujukan UUID filesystem ditandai 'tidak diketahui'.")
    else:
        for part in ctx.opaque:
            info = fsinfo.get(part.id)
            what = f"berisi {info.fs_type}" if info else "filesystem-nya tidak dikenali"
            notes.append(
                f"Partisi {part.id} {what}, jadi UUID filesystem di dalamnya tidak terlihat. "
                "Rujukan ke UUID yang tidak ditemukan ditandai 'tidak diketahui', bukan 'sudah tidak ada'."
            )
    xbootldr = [p for p in linux_parts if p.gpt_type == gpt_types.XBOOTLDR]
    for part in xbootldr:
        info = fsinfo.get(part.id)
        if info is None or info.fs_type != "vfat":
            notes.append(
                f"Isi partisi {part.id} (XBOOTLDR/boot) tidak bisa dibaca dari Windows, hanya header-nya. "
                "Entri bootloader di dalamnya tidak terlihat."
            )

    order = [e for e in boot_entries if e.order is not None]
    order.sort(key=lambda e: e.order)
    return Analysis(installations, order, esp_refs.fallback if esp_refs else None, notes)


def _windows_health(g: _Group, ctx: _Context, boot_entries: Sequence[BootEntry]) -> list[str]:
    reasons = ["Bagian dari Windows. Semua komponen di sini dikunci dan tidak bisa dipilih."]
    managers = [e for e in boot_entries if e.is_windows_boot_manager]
    if not boot_entries:
        reasons.append("Entri boot belum terbaca (butuh Administrator)")
    elif not managers:
        reasons.append("PERINGATAN: entri Windows Boot Manager tidak ditemukan")
    else:
        manager = managers[0]
        if manager.order == 0:
            reasons.append("Windows Boot Manager ada di urutan boot pertama")
        elif manager.order is not None:
            reasons.append(
                f"Windows Boot Manager ada di urutan boot ke-{manager.order + 1}, bukan pertama, "
                "jadi bootloader lain muncul duluan saat laptop dinyalakan"
            )
        else:
            reasons.append("Entri Windows Boot Manager ada, tapi tidak ada di urutan boot")
    if ctx.esp is not None:
        if ctx.esp.find("EFI\\Microsoft\\Boot\\bootmgfw.efi"):
            reasons.append("File EFI\\Microsoft\\Boot\\bootmgfw.efi ada")
        else:
            reasons.append("PERINGATAN: file EFI\\Microsoft\\Boot\\bootmgfw.efi tidak ditemukan di ESP")
    return reasons
