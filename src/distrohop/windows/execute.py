"""Eksekusi rencana uninstall (M2b). INI satu-satunya modul DistroHop yang menghapus sesuatu.

Pengaman berlapis:
1. Gerbang (`check_gate`): preflight lolos, konfirmasi manual sudah diberikan, dan backup lokal + salinan di
   drive eksternal diverifikasi ulang serta masih mencerminkan kondisi ESP & disk saat ini.
2. Runner khusus tahap ini: selain read-only, hanya `bcdedit /set {fwbootmgr} displayorder {bootmgr} /addfirst`
   dan `bcdedit /delete` untuk GUID yang ada di rencana.
3. Penjaga path ESP: EFI\\Microsoft tidak pernah disentuh; di EFI\\Boot hanya bootx64.efi yang boleh diganti,
   dan hanya lewat langkah pengembalian fallback.
4. Setiap langkah dicek ulang sesaat sebelum dijalankan (entri masih sama, tanda tangan masih valid, isi folder
   masih sama dengan backup) dan hasilnya dicek sesudahnya.
5. Gagal = berhenti. Tidak ada langkah yang dicoba ulang dengan cara yang lebih "kuat".
6. Journal mencatat setiap langkah sebelum & sesudah dijalankan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .backup import sha256_file, verify_backup_dir
from .bootentries import BOOTMGR_ID, FWBOOTMGR_ID, load_firmware
from .esp import norm_path
from .journal import Journal
from .preflight import FAIL, PreflightResult
from .runner import AllowListRunner, Runner, powershell_args
from .scanner import ScanReport
from .signature import Signature, check_signature
from .uninstall_plan import PlannedAction, UninstallPlan

log = logging.getLogger(__name__)

# Simulasi crash untuk skenario VM 7. Hanya aktif kalau DISTROHOP_DEV=1 juga di-set.
DEV_FLAG_ENV = "DISTROHOP_DEV"
FAIL_AFTER_ENV = "DISTROHOP_FAIL_AFTER"

SET_WINDOWS_FIRST = ["bcdedit", "/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"]
BOOTMGFW = "EFI\\Microsoft\\Boot\\bootmgfw.efi"
FALLBACK = "EFI\\Boot\\bootx64.efi"
WELL_KNOWN_IDS = frozenset(
    {
        BOOTMGR_ID,
        FWBOOTMGR_ID,
        "{bootmgr}",
        "{fwbootmgr}",
        "{current}",
        "{default}",
        "{memdiag}",
        "{ntldr}",
        "{globalsettings}",
        "{badmemory}",
        "{dbgsettings}",
        "{emssettings}",
        "{bootloadersettings}",
        "{resumeloadersettings}",
        "{hypervisorsettings}",
        "{ramdiskoptions}",
        "{7ea2e1ac-2e61-4728-aaa3-896d9d0a9f0e}",  # {globalsettings}
    }
)

BITLOCKER_SUSPEND_SCRIPT = r"""# distrohop:suspend-bitlocker
$ErrorActionPreference = 'Stop'
$null = Suspend-BitLocker -MountPoint 'C:' -RebootCount 1
"""

_M3_BLOCK = "baru tersedia di M3"


class ExecutionError(RuntimeError):
    pass


class SimulatedCrash(RuntimeError):
    """Crash buatan untuk test VM: langkah sudah dijalankan tapi belum dicatat selesai di journal."""


def _exe(args: Sequence[str]) -> str:
    return Path(args[0]).stem.lower() if args else ""


def execution_runner(inner: Runner, plan: UninstallPlan) -> AllowListRunner:
    """Runner tahap eksekusi: read-only + perintah pengubah yang persis ada di rencana."""
    allowed_ids = {
        a.data["bcd_id"].lower() for a in plan.actions if a.kind == "delete-boot-entry" and a.data.get("bcd_id")
    } - WELL_KNOWN_IDS

    def set_windows_first(args: Sequence[str]) -> bool:
        return _exe(args) == "bcdedit" and [a.lower() for a in args[1:]] == SET_WINDOWS_FIRST[1:]

    def delete_planned_entry(args: Sequence[str]) -> bool:
        return _exe(args) == "bcdedit" and len(args) == 3 and args[1].lower() == "/delete" and args[2].lower() in allowed_ids

    return AllowListRunner(
        inner, extra_cmdlets=frozenset({"suspend-bitlocker"}), rules=(set_windows_first, delete_planned_entry)
    )


def safe_esp_path(root: Path, rel: str, *, fallback_target: bool = False) -> Path:
    """Path absolut di dalam ESP yang boleh diubah, atau ExecutionError."""
    norm = norm_path(rel)
    low = norm.lower()
    if not norm or low in ("efi", "efi\\boot", "efi\\microsoft") or low.startswith("efi\\microsoft\\"):
        raise ExecutionError(f"{rel}: dilindungi, tidak boleh diubah")
    if low.startswith("efi\\boot\\") and not (fallback_target and low == FALLBACK.lower()):
        raise ExecutionError(f"{rel}: file fallback firmware hanya boleh diganti lewat langkah pengembalian fallback")
    base = root.resolve()
    target = (base / Path(*norm.split("\\"))).resolve()
    if target == base or base not in target.parents:
        raise ExecutionError(f"{rel}: berada di luar ESP")
    return target


def _rel(root: Path, path: Path) -> str:
    return "\\".join(path.relative_to(root.resolve()).parts)


def _files_under(target: Path) -> list[Path]:
    return [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())


def _check_covered_by_backup(root: Path, target: Path, hashes: dict[str, str]) -> int:
    """Pastikan setiap file yang akan dihapus ada di backup dengan isi yang sama persis."""
    files = _files_under(target)
    for path in files:
        rel = _rel(root, path)
        expected = hashes.get(rel.lower())
        if expected is None:
            raise ExecutionError(f"{rel} tidak ada di backup; buat backup baru dulu")
        if sha256_file(path) != expected:
            raise ExecutionError(f"{rel} berubah sejak backup dibuat; buat backup baru dulu")
    return len(files)


def _make_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _remove(target: Path) -> None:
    """Hapus file/folder, termasuk file ber-atribut read-only (umum di FAT)."""
    if target.is_dir():
        for path in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            _make_writable(path)
            path.rmdir() if path.is_dir() else path.unlink()
        _make_writable(target)
        target.rmdir()
    else:
        _make_writable(target)
        target.unlink()


@dataclass
class StepResult:
    step: int
    kind: str
    title: str
    ok: bool
    message: str


@dataclass
class ExecutionResult:
    session: str
    journal_path: str
    steps: list[StepResult] = field(default_factory=list)
    completed: bool = False
    stopped: str | None = None


@dataclass
class _Context:
    runner: Runner
    esp_root: Path
    hashes: dict[str, str]
    signature_of: Callable[[Runner, Path], Signature]


def _suspend_bitlocker(action: PlannedAction, ctx: _Context) -> str:
    result = ctx.runner.run(powershell_args(BITLOCKER_SUSPEND_SCRIPT))
    if not result.ok:
        raise ExecutionError(f"Suspend-BitLocker gagal: {result.output() or result.returncode}")
    return "BitLocker ditangguhkan untuk 1 kali restart"


def _set_windows_first(action: PlannedAction, ctx: _Context) -> str:
    result = ctx.runner.run(SET_WINDOWS_FIRST)
    if not result.ok:
        raise ExecutionError(f"bcdedit gagal: {result.output() or result.returncode}")
    config, _ = load_firmware(ctx.runner)
    if not config.display_order or config.display_order[0] != BOOTMGR_ID:
        raise ExecutionError("Setelah perintah dijalankan, Windows Boot Manager belum juga di urutan pertama")
    return "Windows Boot Manager sekarang urutan pertama (dicek ulang)"


def _restore_fallback(action: PlannedAction, ctx: _Context) -> str:
    source = ctx.esp_root / Path(*BOOTMGFW.split("\\"))
    target = safe_esp_path(ctx.esp_root, action.data.get("target", FALLBACK), fallback_target=True)
    if not source.is_file():
        raise ExecutionError(f"{BOOTMGFW} tidak ditemukan")
    signature = ctx.signature_of(ctx.runner, source)
    if not signature.is_microsoft:
        raise ExecutionError(f"Tanda tangan {BOOTMGFW} tidak valid: {signature.describe()}")
    if target.exists():
        rel = _rel(ctx.esp_root, target)
        if ctx.hashes.get(rel.lower()) != sha256_file(target):
            raise ExecutionError(f"{rel} berubah sejak backup dibuat; buat backup baru dulu")
    source_hash = sha256_file(source)
    temporary = target.with_name(target.name + ".distrohop-baru")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != source_hash:
        temporary.unlink()
        raise ExecutionError("Salinan sementara bootmgfw.efi tidak sama dengan aslinya")
    if target.exists():
        _make_writable(target)
    os.replace(temporary, target)
    if sha256_file(target) != source_hash:
        raise ExecutionError(f"{FALLBACK} tidak sama dengan bootmgfw.efi setelah disalin")
    return f"{FALLBACK} sekarang identik dengan bootmgfw.efi (sha256 {source_hash[:16]}…)"


def _delete_boot_entry(action: PlannedAction, ctx: _Context) -> str:
    bcd_id = (action.data.get("bcd_id") or "").lower()
    if not bcd_id or bcd_id in WELL_KNOWN_IDS:
        raise ExecutionError(f"GUID entri tidak boleh dihapus: {bcd_id or '(kosong)'}")
    config, _ = load_firmware(ctx.runner)
    entry = config.find(bcd_id)
    if entry is None:
        raise ExecutionError("Entri tidak ditemukan lagi; kondisi berubah sejak rencana dibuat")
    if entry.is_windows_boot_manager:
        raise ExecutionError("Entri ini adalah Windows Boot Manager")
    if (entry.description or "") != (action.data.get("description") or "") or (entry.path or "").lower() != (
        action.data.get("path") or ""
    ).lower():
        raise ExecutionError(f"Entri berubah sejak rencana dibuat: sekarang '{entry.description}' → {entry.path}")
    result = ctx.runner.run(["bcdedit", "/delete", bcd_id])
    if not result.ok:
        raise ExecutionError(f"bcdedit /delete gagal: {result.output() or result.returncode}")
    config, _ = load_firmware(ctx.runner)
    if config.find(bcd_id) is not None:
        raise ExecutionError("Entri masih ada setelah dihapus")
    return f"Entri '{entry.description}' ({bcd_id}) dihapus (dicek ulang)"


def _delete_esp_item(action: PlannedAction, ctx: _Context) -> str:
    rel = action.data.get("path") or ""
    target = safe_esp_path(ctx.esp_root, rel)
    if not target.exists():
        raise ExecutionError(f"{rel} tidak ditemukan")
    count = _check_covered_by_backup(ctx.esp_root, target, ctx.hashes)
    _remove(target)
    if target.exists():
        raise ExecutionError(f"{rel} masih ada setelah dihapus")
    return f"{rel} dihapus ({count} file, semuanya ada di backup)"


_HANDLERS: dict[str, Callable[[PlannedAction, _Context], str]] = {
    "suspend-bitlocker": _suspend_bitlocker,
    "set-windows-first": _set_windows_first,
    "restore-fallback": _restore_fallback,
    "delete-boot-entry": _delete_boot_entry,
    "delete-esp-item": _delete_esp_item,
}


def manifest_hashes(manifest: dict) -> dict[str, str]:
    return {e["path"].lower(): e["sha256"] for e in manifest.get("esp_files", [])}


def execute_plan(
    plan: UninstallPlan,
    *,
    runner: Runner,
    esp_root: Path,
    manifest: dict,
    journal: Journal,
    suspend_bitlocker: bool = False,
    signature_of: Callable[[Runner, Path], Signature] = check_signature,
    progress: Callable[[str], None] | None = None,
    fail_after: str | None = None,
) -> ExecutionResult:
    say = progress or (lambda message: None)
    actions = list(plan.actions)
    if suspend_bitlocker:
        actions.insert(
            0,
            PlannedAction(
                "suspend-bitlocker",
                "Tangguhkan BitLocker untuk 1 kali restart",
                ["Suspend-BitLocker -MountPoint C: -RebootCount 1"],
            ),
        )
    ctx = _Context(runner, Path(esp_root), manifest_hashes(manifest), signature_of)
    result = ExecutionResult(session=journal.path.stem, journal_path=str(journal.path))

    for number, action in enumerate(actions, 1):
        say(f"[{number}/{len(actions)}] {action.title}…")
        handler = _HANDLERS.get(action.kind)
        journal.record(number, action.kind, action.ref, "started")
        try:
            if handler is None:
                raise ExecutionError(f"Jenis aksi tidak dikenal: {action.kind}")
            message = handler(action, ctx)
        except Exception as exc:  # gagal = berhenti, apa pun penyebabnya
            log.exception("Langkah %s (%s) gagal", number, action.kind)
            text = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ExecutionError) else str(exc)
            journal.record(number, action.kind, action.ref, "failed", text)
            result.steps.append(StepResult(number, action.kind, action.title, False, text))
            result.stopped = f"Berhenti di langkah {number} ({action.title}): {text}"
            journal.finish(False, result.stopped)
            return result
        if fail_after and fail_after in (str(number), action.kind):
            raise SimulatedCrash(f"Simulasi crash setelah langkah {number} ({action.kind}), sebelum dicatat selesai")
        journal.record(number, action.kind, action.ref, "done", message)
        result.steps.append(StepResult(number, action.kind, action.title, True, message))

    result.completed = True
    journal.finish(True, "Semua langkah selesai")
    return result


# --- Gerbang sebelum eksekusi -----------------------------------------------


@dataclass
class GateResult:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    local_dir: Path | None = None
    external_dir: Path | None = None
    manifest: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def check_gate(
    plan: UninstallPlan,
    report: ScanReport,
    preflight: PreflightResult,
    *,
    backup_id: str,
    local_root: Path,
    external_roots: Iterable[Path],
    confirmed: set[str],
) -> GateResult:
    gate = GateResult()
    problems = gate.problems

    if not plan.actions:
        problems.append("Rencana tidak berisi aksi apa pun")
    for item in plan.blocked:
        if _M3_BLOCK in item:
            gate.notes.append(f"Dilewati (M3): {item}")
        else:
            problems.append(f"Pilihan berisi item yang diblokir: {item}")

    for key in ("admin", "layout", "windows-boot-manager"):
        check = preflight.get(key)
        if check is None or check.status == FAIL:
            problems.append(f"Preflight gagal: {check.title if check else key}: {check.message if check else '-'}")
    for check in preflight.needs_confirmation:
        if check.key not in confirmed:
            problems.append(f"Belum dikonfirmasi: {check.title}")

    local = Path(local_root) / backup_id
    gate.local_dir = local
    if not (local / "manifest.json").exists():
        problems.append(f"Backup lokal {local} tidak ditemukan")
        return gate
    failed = [v for v in verify_backup_dir(local) if not v.ok]
    if failed:
        problems.append(f"Backup lokal tidak lolos verifikasi: {failed[0].name}: {failed[0].message}")
    manifest = json.loads((local / "manifest.json").read_text(encoding="utf-8"))
    gate.manifest = manifest

    if not manifest.get("usb_copy"):
        problems.append("Backup ini dibuat dengan --local-only. Eksekusi butuh backup yang punya salinan di drive eksternal")
    else:
        candidates = [Path(root) / "DistroHop" / "backups" / backup_id for root in external_roots]
        external = next((c for c in candidates if (c / "manifest.json").exists()), None)
        if external is None:
            problems.append("Salinan backup di drive eksternal tidak ditemukan; colok drive tempat backup ini disalin")
        else:
            gate.external_dir = external
            failed = [v for v in verify_backup_dir(external) if not v.ok]
            if failed:
                problems.append(f"Salinan eksternal tidak lolos verifikasi: {failed[0].name}: {failed[0].message}")

    if report.inventory is None or manifest.get("layout_fingerprint") != report.inventory.fingerprint():
        problems.append("Layout disk berubah sejak backup dibuat; buat backup baru")
    if report.esp is None:
        problems.append("ESP tidak terbaca")
    else:
        backed_up = manifest_hashes(manifest)
        current = {f.path.lower(): f.sha256 for f in report.esp.files}
        if backed_up != current:
            changed = sorted(set(backed_up.items()) ^ set(current.items()))
            problems.append(f"Isi ESP berubah sejak backup dibuat (mis. {changed[0][0]}); buat backup baru")

    entries_file = local / "firmware-entries.json"
    backed_entries = json.loads(entries_file.read_text(encoding="utf-8")) if entries_file.exists() else []
    backed_ids = {(e.get("bcd_id") or "").lower() for e in backed_entries}
    for action in plan.actions:
        if action.kind == "delete-boot-entry" and action.data.get("bcd_id", "").lower() not in backed_ids:
            problems.append(f"Entri {action.data.get('bcd_id')} tidak tercatat di backup; buat backup baru")
    return gate
