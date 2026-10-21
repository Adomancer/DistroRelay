"""Entri boot firmware dari `bcdedit /enum firmware /v`, digabung dengan pembacaan NVRAM.

Parsing tidak bergantung pada bahasa Windows: identitas diambil dari GUID dan path `.efi`,
dan objek khusus dikenali lewat GUID baku ({fwbootmgr}, {bootmgr}). Nama field bahasa Inggris
hanya dipakai kalau ada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .nvram import NvramSnapshot
from .runner import Runner

FWBOOTMGR_ID = "{a5a30fa2-3d06-4e9f-b5f4-a01df9d1fcba}"
BOOTMGR_ID = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
_ALIASES = {"{fwbootmgr}": FWBOOTMGR_ID, "{bootmgr}": BOOTMGR_ID}

_GUID = re.compile(r"^\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}$")
_EFI_PATH = re.compile(r"^\\.*\.efi$", re.IGNORECASE)
_DEVICE = re.compile(r"^(partition|boot|locate|unknown|ramdisk|vhd|file)=|\\Device\\", re.IGNORECASE)
_LOCALE = re.compile(r"^[a-z]{2,3}-[A-Za-z]{2,4}$")
_KEY_VALUE = re.compile(r"^(\S(?:.*?\S)?)\s{2,}(\S.*?)\s*$")
_DASHES = re.compile(r"^\s*-{3,}\s*$")
_YES_NO_NUMBER = re.compile(r"^(yes|no|\d+)$", re.IGNORECASE)

WINDOWS_BOOTMGR_PATH = "\\efi\\microsoft\\boot\\bootmgfw.efi"


class BcdError(RuntimeError):
    pass


def _canonical_id(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.strip().lower()
    return _ALIASES.get(lowered, lowered)


def _is_guid(value: str) -> bool:
    return bool(_GUID.match(value)) or value.strip().lower() in _ALIASES


@dataclass
class BcdBlock:
    title: str
    values: list[tuple[str, list[str]]]

    def get(self, key: str) -> list[str] | None:
        for name, values in self.values:
            if name.lower() == key:
                return values
        return None

    def first(self, key: str) -> str | None:
        values = self.get(key)
        return values[0] if values else None

    def flat(self) -> list[tuple[str, str]]:
        return [(name, v) for name, values in self.values for v in values]


def parse_blocks(text: str) -> list[BcdBlock]:
    """Pecah output bcdedit jadi blok: judul, garis '----', lalu baris key/value."""
    lines = text.replace("\r", "").split("\n")
    dash_lines = [i for i, line in enumerate(lines) if i > 0 and _DASHES.match(line) and lines[i - 1].strip()]
    blocks: list[BcdBlock] = []
    for n, dash in enumerate(dash_lines):
        end = dash_lines[n + 1] - 1 if n + 1 < len(dash_lines) else len(lines)
        values: list[tuple[str, list[str]]] = []
        for line in lines[dash + 1 : end]:
            if not line.strip():
                continue
            if line[0].isspace():
                if values:
                    values[-1][1].append(line.strip())
                continue
            match = _KEY_VALUE.match(line)
            values.append((match.group(1), [match.group(2)]) if match else (line.strip(), []))
        blocks.append(BcdBlock(lines[dash - 1].strip(), values))
    return blocks


@dataclass
class FirmwareEntry:
    identifier: str
    description: str | None
    path: str | None
    device: str | None
    title: str

    @property
    def is_windows_boot_manager(self) -> bool:
        return self.identifier == BOOTMGR_ID or (self.path or "").lower() == WINDOWS_BOOTMGR_PATH


@dataclass
class FirmwareConfig:
    display_order: list[str] = field(default_factory=list)
    boot_sequence: list[str] = field(default_factory=list)
    timeout: str | None = None
    entries: list[FirmwareEntry] = field(default_factory=list)

    def find(self, identifier: str | None) -> FirmwareEntry | None:
        wanted = _canonical_id(identifier)
        return next((e for e in self.entries if e.identifier == wanted), None)


def _block_identifier(block: BcdBlock) -> str | None:
    explicit = block.first("identifier")
    if explicit:
        return _canonical_id(explicit)
    return next((_canonical_id(v) for _, v in block.flat() if _is_guid(v)), None)


def _guess_description(block: BcdBlock, path: str | None) -> str | None:
    """Tebakan tanpa nama field: nilai teks bebas pertama setelah path (urutan bcdedit tetap)."""
    seen_path = path is None
    for _, value in block.flat():
        if not seen_path:
            seen_path = value == path
            continue
        if _is_guid(value) or _EFI_PATH.match(value) or _DEVICE.search(value):
            continue
        if _LOCALE.match(value) or _YES_NO_NUMBER.match(value):
            continue
        return value
    return None


def _entry_from_block(block: BcdBlock) -> FirmwareEntry:
    flat_values = [v for _, v in block.flat()]
    path = block.first("path") or next((v for v in flat_values if _EFI_PATH.match(v)), None)
    device = block.first("device") or next((v for v in flat_values if _DEVICE.search(v)), None)
    description = block.first("description") or _guess_description(block, path)
    return FirmwareEntry(_block_identifier(block) or "?", description, path, device, block.title)


def _guid_list_after_identifier(block: BcdBlock) -> list[str]:
    """displayorder tanpa nama field: key pertama (setelah identifier) yang nilainya GUID."""
    for name, values in block.values[1:]:
        if values and all(_is_guid(v) for v in values):
            return [_canonical_id(v) for v in values]
    return []


def parse_firmware(text: str) -> FirmwareConfig:
    config = FirmwareConfig()
    for block in parse_blocks(text):
        if _block_identifier(block) == FWBOOTMGR_ID:
            order = block.get("displayorder")
            config.display_order = [_canonical_id(v) for v in order] if order else _guid_list_after_identifier(block)
            config.boot_sequence = [_canonical_id(v) for v in block.get("bootsequence") or []]
            config.timeout = block.first("timeout")
        else:
            config.entries.append(_entry_from_block(block))
    return config


def load_firmware(runner: Runner) -> tuple[FirmwareConfig, str]:
    result = runner.run(["bcdedit", "/enum", "firmware", "/v"])
    if not result.ok:
        raise BcdError(f"bcdedit /enum firmware gagal: {result.output() or result.returncode}")
    return parse_firmware(result.stdout), result.stdout


# --- Gabungan bcdedit + NVRAM -----------------------------------------------


@dataclass
class BootEntry:
    key: str
    description: str
    path: str | None
    partition_guid: str | None
    bcd_id: str | None
    nvram_number: int | None
    order: int | None  # posisi di urutan boot, 0 = pertama
    active: bool | None
    sources: list[str]
    device: str | None

    @property
    def label(self) -> str:
        if self.nvram_number is not None:
            return f"Boot{self.nvram_number:04X}"
        return self.bcd_id or "?"

    @property
    def is_windows_boot_manager(self) -> bool:
        return self.bcd_id == BOOTMGR_ID or (self.path or "").lower() == WINDOWS_BOOTMGR_PATH


def _same(a: str | None, b: str | None) -> bool:
    # Firmware sering menyimpan nama dengan spasi di akhir ("UEFI VBOX HARDDISK VB…-… "), bcdedit tidak.
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _match_bcd(fw: FirmwareConfig, nv: NvramSnapshot, unmatched_bcd: list) -> dict[int, object]:
    """Pasangkan tiap Boot#### dengan objek bcdedit-nya, dari bukti terkuat ke terlemah.

    1. BCDOBJECT di optional data. Kalau satu objek diklaim beberapa Boot#### (firmware/Windows bisa
       membuat entri dobel), hanya yang posisinya sejajar di urutan boot yang dipasangkan. displayorder
       {fwbootmgr} adalah cermin BootOrder, jadi posisinya sama.
    2. Nama + path sama. Kalau kandidatnya lebih dari satu, pilih yang posisinya sejajar.
    3. Tanpa path: nama sama, hanya kalau kandidatnya tepat satu (atau satu yang posisinya sejajar).
    Tiap aturan dijalankan untuk semua entri dulu sebelum aturan berikutnya, supaya bukti lemah pada
    satu entri tidak merebut objek yang punya bukti kuat untuk entri lain.
    """
    claims: dict[str, int] = {}
    for option in nv.options.values():
        if option.bcd_object:
            claims[option.bcd_object] = claims.get(option.bcd_object, 0) + 1

    def aligned(number: int, entry) -> bool:
        position = nv.position(number)
        return position is not None and fw.display_order[position : position + 1] == [entry.identifier]

    def by_object(number, option):
        found = [e for e in unmatched_bcd if option.bcd_object and e.identifier == option.bcd_object]
        if found and claims[option.bcd_object] > 1 and not aligned(number, found[0]):
            return []
        return found

    def by_name_and_path(number, option):
        return [e for e in unmatched_bcd if _same(e.description, option.description) and _same(e.path, option.file_path)]

    def by_name_without_path(number, option):
        if option.file_path:
            return []
        found = [e for e in unmatched_bcd if _same(e.description, option.description) and not e.path]
        return found if len(found) == 1 else [e for e in found if aligned(number, e)]

    matches: dict[int, object] = {}
    for rule in (by_object, by_name_and_path, by_name_without_path):
        for number in sorted(nv.options):
            if number in matches:
                continue
            found = rule(number, nv.options[number])
            match = next((e for e in found if aligned(number, e)), found[0] if found else None)
            if match is not None:
                matches[number] = match
                unmatched_bcd.remove(match)
    return matches


def merge_entries(fw: FirmwareConfig | None, nv: NvramSnapshot | None) -> tuple[list[BootEntry], list[str]]:
    """Satukan dua sumber. Ketidakcocokan dilaporkan sebagai catatan, bukan disembunyikan."""
    entries: list[BootEntry] = []
    notes: list[str] = []
    unmatched_bcd = list(fw.entries) if fw else []

    if nv is not None:
        matches = _match_bcd(fw, nv, unmatched_bcd) if fw is not None else {}
        for number in sorted(nv.options):
            option = nv.options[number]
            match = matches.get(number)
            sources = ["NVRAM", "bcdedit"] if match else ["NVRAM"]
            entries.append(
                BootEntry(
                    key=f"nv:{number:04X}",
                    description=option.description or (match.description if match else "") or option.label,
                    path=option.file_path or (match.path if match else None),
                    partition_guid=option.partition_guid,
                    bcd_id=match.identifier if match else option.bcd_object,
                    nvram_number=number,
                    order=nv.position(number),
                    active=option.active,
                    sources=sources,
                    device=option.device_summary or None,
                )
            )
            if fw is not None and match is None:
                notes.append(f"Entri {option.label} '{option.description}' terlihat di NVRAM tapi tidak di bcdedit")

    for entry in unmatched_bcd:
        order = fw.display_order.index(entry.identifier) if entry.identifier in fw.display_order else None
        if nv is not None:
            notes.append(f"Entri '{entry.description}' ({entry.identifier}) terlihat di bcdedit tapi tidak di NVRAM")
            order = None  # urutan resmi diambil dari BootOrder NVRAM
        entries.append(
            BootEntry(
                key=f"bcd:{entry.identifier}",
                description=entry.description or entry.identifier,
                path=entry.path,
                partition_guid=None,
                bcd_id=entry.identifier,
                nvram_number=None,
                order=order,
                active=None,
                sources=["bcdedit"],
                device=entry.device,
            )
        )

    entries.sort(key=lambda e: (e.order is None, e.order if e.order is not None else 0, e.key))
    return entries, notes
