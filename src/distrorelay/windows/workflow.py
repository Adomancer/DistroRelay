"""Alur uninstall & pemulihan (M2c): satu-satunya jalan dari pilihan pengguna ke perubahan sistem, dipakai CLI dan TUI.

UI hanya bertanya dan menampilkan. Urutan langkah dan syaratnya dipaksa di sini:

1. Sesi terputus yang belum dituntaskan harus diakui dengan mengetik LANJUT.
2. Mode "vm" hanya berlaku kalau mesinnya memang terdeteksi sebagai VM; di mesin fisik mode dipaksa "asli",
   yang butuh dua pernyataan (skenario VM lolos, RECOVERY sudah dilatih). Mode tidak pernah melonggarkan gerbang.
3. Eksekusi hanya menerima `GateTicket` dari `authorize()`: diterbitkan sesudah gerbang lolos dan HAPUS diketik.
   Tiket mengikat hash rencana, ID + hash manifest backup, konfirmasi manual, dan mode. Langkah apa pun yang
   mengubah keadaan (scan, pilih, mode, preflight, konfirmasi, backup, gerbang) membatalkan tiket yang ada, dan
   tiket hanya bisa dipakai sekali.

Pemulihan (`RestoreFlow`) memakai disiplin yang sama: backup diverifikasi ulang, lalu `RestoreTicket` sekali pakai
diterbitkan sesudah PULIHKAN diketik, terikat ke backup dan item yang dipilih.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..format import human_size
from . import disks, flush, gpt_types
from .backup import BackupResult, Verification, backup_runner, backups_root, create_backup, verify_backup_dir
from .bootrefs import Component, Installation
from .esp import norm_path
from .execute import DEV_FLAG_ENV, FAIL_AFTER_ENV, ExecutionResult, GateResult, check_gate, execute_plan, execution_runner
from .journal import Journal, SessionSummary, incomplete_sessions, journal_dir
from .machine import Machine
from .postcheck import post_check
from .preflight import PreflightResult, external_volumes
from .restore import (
    describe_entry,
    entry_label,
    partition_restore_refusal,
    partition_restore_script,
    protected_esp_item,
    restore_boot_entry,
    restore_esp_items,
    restore_runner,
    select_backup_entries,
    unrestorable_reason,
)
from .runner import ReadOnlyRunner, Runner, powershell_args
from .scanner import ScanReport
from .stopping import StopSignal
from .uninstall_plan import SELECTABLE_KINDS, UninstallPlan, build_plan, expand_selection

CONTINUE_WORD = "LANJUT"
DELETE_WORD = "HAPUS"
RESTORE_WORD = "PULIHKAN"
# Partisi yang mungkin berisi data pribadi (root, home, data, tidak dikenal): backup Linux DistroRelay baru ada di M5,
# jadi syaratnya pernyataan yang diketik (plan: aturan backup bertingkat).
DATA_WORD = "DATA SUDAH DIBACKUP"


@dataclass(frozen=True)
class TypedConfirmation:
    """Kata yang wajib diketik persis sebelum tiket eksekusi terbit (selain HAPUS)."""

    key: str
    title: str
    prompt: str
    word: str

# Syarat proses dari plan yang tidak bisa dicek kode; wajib dinyatakan di mode "asli".
REAL_MACHINE_STATEMENTS = {
    "vm-scenarios": "Semua skenario VM untuk M2b di tests/vm/SCENARIOS.md sudah lolos dengan kode versi ini?",
    "recovery-practiced": "Sudah latihan prosedur docs/RECOVERY.md di VM (skenario 8)?",
}


def typed(value: str | None, word: str) -> bool:
    """Kata konfirmasi harus persis (peka huruf besar-kecil). Hanya spasi di ujung yang ditoleransi."""
    return (value or "").rstrip() == word


class FlowError(RuntimeError):
    """Langkah dipanggil di luar urutan, atau tiket tidak berlaku."""


def _default_external_roots(report: ScanReport) -> list[Path]:
    return [Path(f"{p.drive_letter}:\\") for p in external_volumes(report.inventory)] if report.inventory else []


def fail_after_from_env() -> str | None:
    return os.environ.get(FAIL_AFTER_ENV) if os.environ.get(DEV_FLAG_ENV) == "1" else None


def _load_inventory(runner: Runner) -> disks.DiskInventory:
    return disks.load_inventory(runner)[0]


@dataclass
class FlowDeps:
    scan: Callable[..., ScanReport]  # scan(progress=...) -> ScanReport
    inner_runner: Runner  # dibungkus runner allowlist per tahap; UI tidak pernah memegangnya
    run_preflight: Callable[[ScanReport], PreflightResult]
    mount_esp: Callable
    detect_machine: Callable[[], Machine]
    read_nvram: Callable
    flush_bcd: Callable[[], None] = flush.flush_bcd
    flush_volume: Callable[[Path], None] = flush.flush_volume
    backups_root: Path = field(default_factory=backups_root)
    journal_dir: Path = field(default_factory=journal_dir)
    external_roots: Callable[[ScanReport], list[Path]] = _default_external_roots
    signature_of: Callable | None = None
    fail_after: str | None = None
    load_inventory: Callable[[Runner], disks.DiskInventory] = _load_inventory


def default_deps() -> FlowDeps:
    """Ketergantungan sungguhan untuk Windows."""
    from . import disks, esp, nvram
    from .machine import detect_machine
    from .preflight import run_preflight
    from .runner import ReadOnlyRunner, SubprocessRunner
    from .scanner import run_scan

    inner = SubprocessRunner()

    def preflight(report: ScanReport) -> PreflightResult:
        runner = backup_runner(inner)
        fresh, _ = disks.load_inventory(runner)
        return run_preflight(report, runner, fresh_inventory=fresh, mount_esp=esp.mounted_esp)

    return FlowDeps(
        scan=lambda progress=None: run_scan(progress=progress),
        inner_runner=inner,
        run_preflight=preflight,
        mount_esp=esp.mounted_esp,
        detect_machine=lambda: detect_machine(ReadOnlyRunner(inner)),
        read_nvram=nvram.read_nvram,
        fail_after=fail_after_from_env(),
    )


def plan_hash(plan: UninstallPlan) -> str:
    content = {
        "actions": [[a.kind, a.ref, a.commands, a.data] for a in plan.actions],
        "blocked": plan.blocked,
        "selected": plan.selected,
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def _manifest_hash(manifest: dict | None) -> str:
    return hashlib.sha256(json.dumps(manifest or {}, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class BackupInfo:
    backup_id: str
    created_at: str
    usb_copy: bool  # False = dibuat dengan --local-only, tidak memenuhi syarat eksekusi

    def describe(self) -> str:
        where = "lokal + drive eksternal" if self.usb_copy else "lokal saja (--local-only)"
        return f"{self.backup_id}  ({where})"


def list_backups(root: Path) -> list[BackupInfo]:
    """Backup lokal yang punya manifest, terbaru dulu. Isinya belum diverifikasi."""
    found = []
    for path in Path(root).glob("*"):
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found.append(BackupInfo(path.name, str(manifest.get("created_at") or ""), bool(manifest.get("usb_copy"))))
    return sorted(found, key=lambda b: b.backup_id, reverse=True)


@dataclass(frozen=True)
class Candidate:
    """Satu komponen yang bisa ditampilkan di layar pilih. `blocked` = alasan tidak bisa dipilih (None = bisa)."""

    installation: Installation
    component: Component
    blocked: str | None


@dataclass(frozen=True)
class GateTicket:
    plan_hash: str
    backup_id: str
    manifest_hash: str
    confirmed: frozenset[str]
    mode: str
    generation: int


@dataclass(frozen=True)
class ModeDecision:
    mode: str  # mode yang berlaku: "vm" | "asli"
    forced_real: bool  # pengguna memilih vm, tapi mesinnya fisik
    machine: Machine
    statements: dict[str, str]  # pernyataan yang wajib dijawab "ya" di mode ini


class UninstallFlow:
    def __init__(self, deps: FlowDeps):
        self.deps = deps
        self.machine: Machine | None = None
        self.mode: str | None = None
        self.report: ScanReport | None = None
        self.plan: UninstallPlan | None = None
        self.unknown: list[str] = []
        self.preflight: PreflightResult | None = None
        self.confirmed: set[str] = set()
        self.backup_id: str | None = None
        self.backup_result: BackupResult | None = None
        self.gate_result: GateResult | None = None
        self.execution: ExecutionResult | None = None
        self.journal: Journal | None = None
        self._acknowledged = False
        self._ticket: GateTicket | None = None
        self._generation = 0

    def _changed(self) -> None:
        self._generation += 1
        self._ticket = None
        self.gate_result = None

    # --- 1. Sesi terputus ----------------------------------------------------
    def unfinished_sessions(self) -> list[SessionSummary]:
        return incomplete_sessions(self.deps.journal_dir)

    def acknowledge_unfinished(self, word: str) -> bool:
        if typed(word, CONTINUE_WORD):
            self._acknowledged = True
            self._changed()
        return self._acknowledged

    # --- 2. VM atau mesin asli -----------------------------------------------
    def decide_mode(self, choice: str) -> ModeDecision:
        """Mode yang berlaku untuk pilihan pengguna, sebelum pernyataannya dijawab."""
        choice = (choice or "").strip().lower()
        if choice not in ("vm", "asli"):
            raise FlowError("Pilih 'vm' atau 'asli'")
        if self.machine is None:
            self.machine = self.deps.detect_machine()
        mode = "vm" if choice == "vm" and self.machine.is_vm else "asli"
        return ModeDecision(
            mode,
            forced_real=choice == "vm" and mode == "asli",
            machine=self.machine,
            statements={} if mode == "vm" else dict(REAL_MACHINE_STATEMENTS),
        )

    def set_mode(self, choice: str, answers: Mapping[str, bool]) -> list[str]:
        """Tetapkan mode. Hasilnya daftar pernyataan yang belum dijawab "ya" (kosong = mode berlaku)."""
        decision = self.decide_mode(choice)
        missing = [key for key in decision.statements if not answers.get(key)]
        self.mode = None if missing else decision.mode
        self._changed()
        return missing

    # --- 3. Scan & pilih -------------------------------------------------------
    def scan(self, progress: Callable[[str], None] | None = None) -> ScanReport:
        self.report = self.deps.scan(progress=progress)
        self.plan, self.unknown, self.preflight = None, [], None
        self.confirmed, self.backup_id, self.backup_result = set(), None, None
        self._changed()
        return self.report

    def candidates(self) -> list[Candidate]:
        """Komponen instalasi Linux / tak terkait, masing-masing dengan alasan kalau tidak bisa dipilih.

        Alasannya diambil dari rencana itu sendiri (komponen itu sendirian), jadi sama persis dengan yang akan
        diblokir `select()`. Tidak mengubah keadaan alur.
        """
        if self.report is None or self.report.analysis is None:
            raise FlowError("Scan dulu (analisis belum ada)")
        analysis = self.report.analysis
        found = []
        for inst in analysis.installations:
            if inst.kind not in SELECTABLE_KINDS:
                continue
            for comp in inst.components:
                blocked = build_plan(
                    analysis, self.report.boot_entries, [comp.ref],
                    inventory=self.report.inventory, fsinfo=self.report.fsinfo,
                ).blocked
                found.append(Candidate(inst, comp, blocked[0] if blocked else None))
        return found

    def select(self, selectors: Sequence[str], extend_c: bool = False) -> UninstallPlan:
        if self.report is None or self.report.analysis is None:
            raise FlowError("Scan dulu (analisis belum ada)")
        refs, self.unknown = expand_selection(self.report.analysis, list(selectors))
        self.plan = build_plan(
            self.report.analysis, self.report.boot_entries, refs,
            inventory=self.report.inventory, fsinfo=self.report.fsinfo, extend_c=extend_c,
        )
        self._changed()
        return self.plan

    def required_typing(self) -> list[TypedConfirmation]:
        """Kata yang wajib diketik untuk rencana ini selain HAPUS: ID tiap partisi, plus pernyataan backup data."""
        required = []
        for action in self.plan.actions if self.plan else []:
            if action.kind != "delete-partition":
                continue
            pid, role = action.data["id"], action.data.get("role")
            required.append(
                TypedConfirmation(f"id:{pid}", f"ID partisi {pid}", f"Ketik ID partisi yang akan dihapus ({pid})", pid)
            )
            if action.data.get("needs_data_statement"):
                required.append(
                    TypedConfirmation(
                        f"data:{pid}",
                        f"Backup data {pid}",
                        f"Partisi {pid} berperan '{role}', jadi bisa berisi data pribadi. Daftar paket + dotfiles "
                        "BUKAN backup dokumen/foto/kode. Kalau datanya sudah kamu backup sendiri (atau memang tidak "
                        f"perlu), ketik {DATA_WORD}",
                        DATA_WORD,
                    )
                )
        return required

    def typing_problems(self, typed_words: Mapping[str, str]) -> list[str]:
        return [
            f"{t.title}: ketik {t.word} persis"
            for t in self.required_typing()
            if not typed(typed_words.get(t.key), t.word)
        ]

    # --- 4. Preflight & konfirmasi manual ------------------------------------
    def run_preflight(self) -> PreflightResult:
        if self.report is None:
            raise FlowError("Scan dulu")
        self.preflight = self.deps.run_preflight(self.report)
        self.confirmed = set()
        self._changed()
        return self.preflight

    def confirm(self, key: str, yes: bool) -> None:
        if self.preflight is None:
            raise FlowError("Preflight dulu")
        if key not in {c.key for c in self.preflight.needs_confirmation}:
            raise FlowError(f"Tidak ada konfirmasi bernama {key}")
        if yes:
            self.confirmed.add(key)
        else:
            self.confirmed.discard(key)
        self._changed()

    # --- 5. Backup ---------------------------------------------------------------
    def backup_targets(self) -> list[Path]:
        return self.deps.external_roots(self.report) if self.report else []

    def make_backup(self, target: Path, progress: Callable[[str], None] | None = None) -> BackupResult:
        """Backup baru ke ESP lokal + `target` (root drive eksternal), lalu dipakai untuk gerbang."""
        if self.report is None or self.preflight is None:
            raise FlowError("Scan dan preflight dulu")
        if Path(target) not in self.backup_targets():
            raise FlowError(f"{target} bukan drive eksternal (disk fisik selain SSD Windows)")
        result = create_backup(
            self.report,
            backup_runner(self.deps.inner_runner, root=self.deps.backups_root),
            self.deps.mount_esp,
            local_root=self.deps.backups_root,
            usb_root=Path(target),
            progress=progress,
        )
        self.backup_result = result
        self.backup_id = result.backup_id if result.ok else None
        self._changed()
        return result

    def existing_backups(self) -> list[BackupInfo]:
        return list_backups(self.deps.backups_root)

    def use_backup(self, backup_id: str) -> None:
        """Pakai backup yang sudah ada (CLI `--backup`). Gerbang tetap memverifikasinya ulang."""
        self.backup_id = backup_id
        self.backup_result = None
        self._changed()

    # --- 6. Gerbang & konfirmasi akhir ----------------------------------------
    def gate(self) -> GateResult:
        missing = [
            name for name, ready in (
                ("scan", self.report is not None), ("rencana", self.plan is not None),
                ("preflight", self.preflight is not None), ("backup", self.backup_id is not None),
            ) if not ready
        ]
        if missing:
            raise FlowError(f"Belum lengkap: {', '.join(missing)}")
        result = check_gate(
            self.plan,
            self.report,
            self.preflight,
            backup_id=self.backup_id,
            local_root=self.deps.backups_root,
            external_roots=self.deps.external_roots(self.report),
            confirmed=self.confirmed,
        )
        if self.mode is None:
            result.problems.append("Mode belum ditetapkan (VM atau laptop asli, beserta pernyataannya)")
        if self.unknown:
            result.problems.append(f"Pilihan tidak dikenal: {', '.join(self.unknown)}")
        if self.unfinished_sessions() and not self._acknowledged:
            result.problems.append(f"Ada sesi terputus yang belum diakui (ketik {CONTINUE_WORD})")
        self._changed()
        self.gate_result = result
        return result

    def authorize(self, word: str, typed_words: Mapping[str, str] | None = None) -> GateTicket | None:
        """Terbitkan tiket eksekusi kalau gerbang lolos, HAPUS diketik persis, dan semua kata wajib lain
        (`required_typing`, mis. ID partisi) juga diketik persis. None = ada yang tidak diketik dengan benar."""
        if self.gate_result is None or not self.gate_result.ok:
            raise FlowError("Gerbang belum lolos")
        if not typed(word, DELETE_WORD) or self.typing_problems(typed_words or {}):
            return None
        self._ticket = GateTicket(
            plan_hash(self.plan),
            self.backup_id,
            _manifest_hash(self.gate_result.manifest),
            frozenset(self.confirmed),
            self.mode,
            self._generation,
        )
        return self._ticket

    # --- 7. Eksekusi & post-check ---------------------------------------------
    def _check_ticket(self, ticket: GateTicket | None) -> None:
        current = self._ticket
        valid = (
            ticket is not None
            and ticket is current
            and self.gate_result is not None
            and self.gate_result.ok
            and ticket.generation == self._generation
            and ticket.plan_hash == plan_hash(self.plan)
            and ticket.backup_id == self.backup_id
            and ticket.manifest_hash == _manifest_hash(self.gate_result.manifest)
            and ticket.confirmed == frozenset(self.confirmed)
            and ticket.mode == self.mode
        )
        if not valid:
            raise FlowError("Tiket eksekusi tidak berlaku: keadaan berubah sejak gerbang; jalankan gerbang lagi")

    def execute(
        self,
        ticket: GateTicket | None,
        *,
        progress: Callable[[str], None] | None = None,
        stop: StopSignal | None = None,
    ) -> ExecutionResult:
        self._check_ticket(ticket)
        self._ticket = None  # sekali pakai
        runner = execution_runner(self.deps.inner_runner, self.plan)
        session = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.journal = Journal.start(
            self.deps.journal_dir,
            session,
            {
                "where": self.mode,
                "machine": self.machine.description if self.machine else "",
                "backup": self.backup_id,
                "select": self.plan.selected,
                "plan_hash": ticket.plan_hash,
                "actions": [a.title for a in self.plan.actions],
            },
        )
        extra = {"signature_of": self.deps.signature_of} if self.deps.signature_of else {}
        with self.deps.mount_esp(runner, self.report.inventory.esp()) as mounted:
            self.execution = execute_plan(
                self.plan,
                runner=runner,
                esp_root=mounted.root,
                manifest=self.gate_result.manifest,
                journal=self.journal,
                suspend_bitlocker=any(v.needs_recovery_key for v in self.report.bitlocker or []),
                read_nvram=self.deps.read_nvram,
                flush_bcd=self.deps.flush_bcd,
                flush_volume=self.deps.flush_volume,
                progress=progress,
                fail_after=self.deps.fail_after,
                stop=stop,
                inventory=self.report.inventory,
                load_inventory=self.deps.load_inventory,
                **extra,
            )
        return self.execution

    def post_check(self, progress: Callable[[str], None] | None = None) -> list[Verification]:
        if self.execution is None or self.journal is None:
            raise FlowError("Eksekusi dulu")
        after = self.deps.scan(progress=progress)
        checks = post_check(self.plan, self.gate_result.manifest, self.report, after)
        self.journal.post_check(all(c.ok for c in checks), [c.name for c in checks if not c.ok])
        return checks


# --- Pemulihan ------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreChoice:
    key: str  # path ESP (EFI\fedora) atau Boot####/GUID entri
    label: str
    blocked: str | None = None  # alasan tidak bisa dipulihkan dari sini (None = bisa)


@dataclass(frozen=True)
class RestoreTicket:
    backup_id: str
    manifest_hash: str
    esp_items: tuple[str, ...]
    entries: tuple[str, ...]
    partitions: tuple[str, ...]
    generation: int


def _durable(label: str, action: Callable[[], None]) -> Verification:
    try:
        action()
    except flush.FlushError as exc:
        return Verification(label, False, str(exc))
    return Verification(label, True, "Ya")


def _partition_at(inventory: disks.DiskInventory, disk_guid: str, part: disks.Partition) -> disks.Partition | None:
    """Partisi di disk `disk_guid` dengan offset, ukuran, dan tipe yang sama dengan `part`."""
    numbers = {d.number for d in inventory.disks if d.guid == disk_guid}
    return next(
        (p for p in inventory.partitions
         if p.disk_number in numbers and (p.offset, p.size, p.gpt_type) == (part.offset, part.size, part.gpt_type)),
        None,
    )


def _esp_group(path: str) -> str:
    """'EFI\\fedora\\shimx64.efi' -> 'EFI\\fedora'; 'grub\\grub.cfg' -> 'grub'; 'mach_kernel' -> 'mach_kernel'."""
    parts = path.split("\\")
    return "\\".join(parts[:2]) if parts[0].lower() == "efi" and len(parts) >= 2 else parts[0]


class RestoreFlow:
    """Pulihkan folder/file ESP, entri boot firmware, dan partisi Linux dari backup DistroRelay (menu Pulihkan)."""

    def __init__(self, deps: FlowDeps):
        self.deps = deps
        self.backup_id: str | None = None
        self.manifest: dict | None = None
        self.records: list[dict] = []
        self.backup_layout: disks.DiskInventory | None = None
        self.backup_fsinfo: dict[str, dict] = {}
        self.current: disks.DiskInventory | None = None
        self.verifications: list[Verification] = []
        self.esp_items: list[str] = []
        self.entries: list[dict] = []
        self.partitions: list[disks.Partition] = []
        self._ticket: RestoreTicket | None = None
        self._generation = 0

    def _changed(self) -> None:
        self._generation += 1
        self._ticket = None

    def backups(self) -> list[BackupInfo]:
        return list_backups(self.deps.backups_root)

    def load(self, backup_id: str) -> list[Verification]:
        """Verifikasi ulang backup lokal, lalu baca isinya. Backup yang gagal verifikasi tidak bisa dipakai."""
        self.backup_id, self.manifest, self.records = None, None, []
        self.backup_layout, self.backup_fsinfo, self.current = None, {}, None
        self.esp_items, self.entries, self.partitions = [], [], []
        self._changed()
        directory = Path(self.deps.backups_root) / (backup_id or "")
        if not backup_id or Path(backup_id).name != backup_id or not (directory / "manifest.json").exists():
            self.verifications = [Verification("Backup", False, f"Backup '{backup_id}' tidak ada di {self.deps.backups_root}")]
            return self.verifications
        self.verifications = verify_backup_dir(directory)
        if all(v.ok for v in self.verifications):
            self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            self.records = json.loads((directory / "firmware-entries.json").read_text(encoding="utf-8"))
            self.backup_layout = disks.parse_inventory((directory / "partitions.json").read_text(encoding="utf-8"))
            fsinfo_file = directory / "fsinfo.json"  # backup sebelum M3 tidak punya file ini
            self.backup_fsinfo = json.loads(fsinfo_file.read_text(encoding="utf-8")) if fsinfo_file.exists() else {}
            self.backup_id = backup_id
            self.refresh_disks()
        return self.verifications

    def refresh_disks(self) -> None:
        """Baca daftar disk sekarang (read-only), untuk menentukan partisi mana yang masih bisa dibuat ulang."""
        try:
            self.current = self.deps.load_inventory(ReadOnlyRunner(self.deps.inner_runner))
        except Exception:  # pilihan partisi jadi tidak tersedia, sisanya tetap bisa
            self.current = None

    def _require_backup(self) -> Path:
        if self.backup_id is None or self.manifest is None:
            raise FlowError("Pilih backup yang lolos verifikasi dulu")
        return Path(self.deps.backups_root) / self.backup_id

    def esp_choices(self) -> list[RestoreChoice]:
        """Folder di EFI\\ dan folder/file di root ESP yang ada di backup."""
        self._require_backup()
        groups: dict[str, list[dict]] = {}
        for entry in self.manifest.get("esp_files", []):
            groups.setdefault(_esp_group(entry["path"]), []).append(entry)
        return [
            RestoreChoice(
                key,
                f"{key}  ({len(files)} file, {human_size(sum(f.get('size', 0) for f in files))})",
                "Milik Windows: pulihkan lewat docs/RECOVERY.md (bcdboot)" if protected_esp_item(key) else None,
            )
            for key, files in sorted(groups.items(), key=lambda item: item[0].lower())
        ]

    def entry_choices(self) -> list[RestoreChoice]:
        self._require_backup()
        return [RestoreChoice(entry_label(r), describe_entry(r), unrestorable_reason(r)) for r in self.records]

    def _describe_partition(self, part: disks.Partition, short: bool = False) -> str:
        info = self.backup_fsinfo.get(part.id) or {}
        if short:
            return f"{part.id} · {human_size(part.size)} · {part.type_name}" + (f" · {info['fs_type']}" if info else "")
        content = f" · {info.get('fs_type')}" + (f" UUID {info['uuid']}" if info.get("uuid") else "") if info else ""
        return f"{part.id} · {human_size(part.size)} · {part.type_name} · offset {part.offset:,}{content}"

    def partition_choices(self) -> list[RestoreChoice]:
        """Partisi Linux yang tercatat di backup. Hanya yang sudah tidak ada dan ruangnya masih kosong yang bisa."""
        self._require_backup()
        return [
            RestoreChoice(part.guid, self._describe_partition(part, short=True),
                          partition_restore_refusal(part, self.backup_layout, self.current))
            for part in self.backup_layout.partitions
            if part.category == gpt_types.LINUX
        ]

    def _find_partition(self, selector: str) -> disks.Partition | None:
        key = (selector or "").strip().strip("{}").lower()
        return next((p for p in self.backup_layout.partitions if key in (p.guid, p.id.lower())), None)

    def select(self, esp_items: Sequence[str], entries: Sequence[str], partitions: Sequence[str] = ()) -> str | None:
        """Tetapkan yang dipulihkan. Hasilnya masalah pertama yang ditemukan (None = pilihan berlaku)."""
        self._require_backup()
        self.esp_items, self.entries, self.partitions = [], [], []
        self._changed()
        chosen, problem = select_backup_entries(self.records, list(entries))
        if problem:
            return problem
        for record in chosen:
            reason = unrestorable_reason(record)
            if reason:
                return f"Entri {describe_entry(record)}: {reason}"
        for item in esp_items:
            if protected_esp_item(item):
                return f"{item or '(kosong)'}: dilindungi; pulihkan Windows lewat docs/RECOVERY.md (bcdboot)"
        parts = []
        for selector in partitions:
            part = self._find_partition(selector)
            if part is None:
                return f"Partisi '{selector}' tidak ada di backup"
            reason = partition_restore_refusal(part, self.backup_layout, self.current)
            if reason:
                return f"Partisi {part.id}: {reason}"
            parts.append(part)
        if not esp_items and not chosen and not parts:
            return "Belum ada yang dipilih"
        self.esp_items = list(dict.fromkeys(norm_path(item) for item in esp_items))
        self.entries = chosen
        self.partitions = list({p.guid: p for p in parts}.values())
        return None

    def summary(self) -> str:
        return (
            f"Akan dipulihkan dari backup {self.backup_id}: "
            f"partisi {', '.join(self._describe_partition(p) for p in self.partitions) or '-'}; "
            f"folder/file {', '.join(self.esp_items) or '-'}; "
            f"entri {', '.join(describe_entry(r) for r in self.entries) or '-'}"
        )

    def authorize(self, word: str) -> RestoreTicket | None:
        """Tiket pemulihan kalau PULIHKAN diketik persis. None = tidak diketik."""
        self._require_backup()
        if not self.esp_items and not self.entries and not self.partitions:
            raise FlowError("Pilih yang dipulihkan dulu")
        if not typed(word, RESTORE_WORD):
            return None
        self._ticket = RestoreTicket(
            self.backup_id,
            _manifest_hash(self.manifest),
            tuple(self.esp_items),
            tuple(entry_label(r) for r in self.entries),
            tuple(p.guid for p in self.partitions),
            self._generation,
        )
        return self._ticket

    def run(self, ticket: RestoreTicket | None, progress: Callable[[str], None] | None = None) -> list[Verification]:
        if ticket is None or ticket is not self._ticket or ticket.generation != self._generation:
            raise FlowError("Tiket pemulihan tidak berlaku: pilihan berubah sejak PULIHKAN diketik")
        self._ticket = None  # sekali pakai
        say = progress or (lambda message: None)
        directory = self._require_backup()

        # Backup dibaca lagi tepat sebelum dipakai: isinya bisa berubah sejak dipilih.
        say("Memverifikasi ulang backup…")
        failed = [v for v in verify_backup_dir(directory) if not v.ok]
        if failed:
            return failed
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if _manifest_hash(manifest) != ticket.manifest_hash:
            return [Verification("Backup", False, "manifest.json berubah sejak backup dipilih")]

        disk_guid = {d.number: d.guid for d in self.backup_layout.disks}
        scripts = frozenset(partition_restore_script(p, disk_guid[p.disk_number]) for p in self.partitions)
        runner, created = restore_runner(self.deps.inner_runner, scripts)
        results: list[Verification] = []
        if self.partitions:
            results += self._restore_partitions(runner, disk_guid, say)
        if self.esp_items:
            esp = self.deps.load_inventory(runner).esp()
            expected = (manifest.get("esp_partition") or {}).get("guid") or ""
            if esp is None:
                results.append(Verification("ESP", False, "ESP tidak ditemukan"))
            elif expected and esp.guid.lower() != expected.lower():
                results.append(
                    Verification("ESP sama dengan saat backup", False, f"GUID ESP sekarang {esp.guid}, di backup {expected}")
                )
            else:
                say("Memulihkan folder/file ESP…")
                with self.deps.mount_esp(runner, esp) as mounted:
                    results += restore_esp_items(directory, self.esp_items, mounted.root)
                    results.append(_durable("Folder/file ESP ditulis ke disk", lambda: self.deps.flush_volume(mounted.root)))
        for record in self.entries:
            say(f"Membuat ulang entri {describe_entry(record)}…")
            results.append(restore_boot_entry(runner, created, record))
        if self.entries:
            results.append(_durable("BCD ditulis ke disk", self.deps.flush_bcd))
        return results

    def _restore_partitions(self, runner: Runner, disk_guid: dict, say: Callable[[str], None]) -> list[Verification]:
        """Buat ulang entri GPT, lalu pastikan isinya utuh: UUID filesystem sama dengan saat backup."""
        results: list[Verification] = []
        restored: list[tuple[disks.Partition, str]] = []  # (partisi di backup, GUID disk)
        for part in self.partitions:
            label = f"Pulihkan partisi {part.id}"
            current = self.deps.load_inventory(runner)  # dicek ulang tepat sebelum dibuat
            reason = partition_restore_refusal(part, self.backup_layout, current)
            if reason:
                results.append(Verification(label, False, reason))
                continue
            say(f"Membuat ulang partisi {part.id}…")
            done = runner.run(powershell_args(partition_restore_script(part, disk_guid[part.disk_number])))
            if not done.ok:
                results.append(Verification(label, False, f"New-Partition gagal: {done.output() or done.returncode}"))
                continue
            after = self.deps.load_inventory(runner)
            found = _partition_at(after, disk_guid[part.disk_number], part)
            if found is None:
                results.append(Verification(label, False, "Partisi tidak terlihat dengan offset/ukuran/tipe yang sama"))
                continue
            results.append(Verification(label, True, f"Dibuat ulang sebagai {found.id} (GUID partisi baru {found.guid})"))
            restored.append((part, disk_guid[part.disk_number]))

        if restored:
            say("Memeriksa isi partisi (scan ulang)…")
            report = self.deps.scan()
            for part, guid in restored:
                expected = (self.backup_fsinfo.get(part.id) or {}).get("uuid")
                found = _partition_at(report.inventory, guid, part) if report.inventory else None
                info = report.fsinfo.get(found.id) if found else None
                label = f"Isi partisi {part.id} utuh"
                if not expected:
                    results.append(Verification(label, True, "UUID filesystem tidak tercatat di backup ini (backup lama); "
                                                             f"sekarang terbaca: {info.describe() if info else 'tidak dikenali'}"))
                elif info is not None and info.uuid == expected:
                    results.append(Verification(label, True, f"UUID filesystem sama ({expected})"))
                else:
                    now = info.uuid if info else "tidak terbaca"
                    results.append(Verification(label, False, f"UUID filesystem {now}, di backup {expected}; "
                                                              "ruangnya mungkin sudah ditimpa"))
        return results
