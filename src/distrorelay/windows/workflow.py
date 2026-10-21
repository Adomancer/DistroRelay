"""Alur uninstall (M2c fase A): satu-satunya jalan dari pilihan pengguna ke eksekusi, dipakai CLI dan TUI.

UI hanya bertanya dan menampilkan. Urutan langkah dan syaratnya dipaksa di sini:

1. Sesi terputus yang belum dituntaskan harus diakui dengan mengetik LANJUT.
2. Mode "vm" hanya berlaku kalau mesinnya memang terdeteksi sebagai VM; di mesin fisik mode dipaksa "asli",
   yang butuh dua pernyataan (skenario VM lolos, RECOVERY sudah dilatih). Mode tidak pernah melonggarkan gerbang.
3. Eksekusi hanya menerima `GateTicket` dari `authorize()`: diterbitkan sesudah gerbang lolos dan HAPUS diketik.
   Tiket mengikat hash rencana, ID + hash manifest backup, konfirmasi manual, dan mode. Langkah apa pun yang
   mengubah keadaan (scan, pilih, mode, preflight, konfirmasi, backup, gerbang) membatalkan tiket yang ada, dan
   tiket hanya bisa dipakai sekali.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import flush
from .backup import BackupResult, Verification, backup_runner, backups_root, create_backup
from .execute import DEV_FLAG_ENV, FAIL_AFTER_ENV, ExecutionResult, GateResult, check_gate, execute_plan, execution_runner
from .journal import Journal, SessionSummary, incomplete_sessions, journal_dir
from .machine import Machine
from .postcheck import post_check
from .preflight import PreflightResult, external_volumes
from .runner import Runner
from .scanner import ScanReport
from .stopping import StopSignal
from .uninstall_plan import UninstallPlan, build_plan, expand_selection

CONTINUE_WORD = "LANJUT"
DELETE_WORD = "HAPUS"
RESTORE_WORD = "PULIHKAN"

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

    def select(self, selectors: Sequence[str]) -> UninstallPlan:
        if self.report is None or self.report.analysis is None:
            raise FlowError("Scan dulu (analisis belum ada)")
        refs, self.unknown = expand_selection(self.report.analysis, list(selectors))
        self.plan = build_plan(self.report.analysis, self.report.boot_entries, refs)
        self._changed()
        return self.plan

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

    def use_backup(self, backup_id: str) -> None:
        """Pakai backup yang sudah ada (CLI `--backup`). Gerbang tetap memverifikasinya ulang."""
        self.backup_id = backup_id
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

    def authorize(self, word: str) -> GateTicket | None:
        """Terbitkan tiket eksekusi kalau gerbang lolos dan HAPUS diketik persis. None = tidak diketik."""
        if self.gate_result is None or not self.gate_result.ok:
            raise FlowError("Gerbang belum lolos")
        if not typed(word, DELETE_WORD):
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
