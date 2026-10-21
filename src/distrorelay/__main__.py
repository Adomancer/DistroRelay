"""Entry point: `python -m distrorelay [scan]`."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from . import __version__
from .logging_setup import setup_logging

log = logging.getLogger("distrorelay")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distrorelay",
        description=(
            "Toolkit distro-hopping untuk dual-boot Windows + Linux. "
            "scan/plan/backup/verify-backup/journal hanya membaca; uninstall dan restore MENGUBAH sistem (coba di VM dulu)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"DistroRelay {__version__}")
    parser.add_argument(
        "--no-elevate", action="store_true", help="jangan minta hak Administrator (scan jadi terbatas)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log lebih detail")
    parser.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tui", help="buka dashboard (default)")
    scan = commands.add_parser("scan", help="scan read-only dan tampilkan ringkasan di terminal")
    scan.add_argument("--json", metavar="FILE", help="simpan laporan lengkap ke file JSON")
    scan.add_argument(
        "--raw-dir",
        metavar="DIR",
        help="simpan output mentah perintah (untuk fixture test; nomor seri disk disamarkan)",
    )
    plan = commands.add_parser("plan", help="dry-run rencana uninstall (tidak menjalankan apa pun)")
    plan.add_argument(
        "--select", action="append", default=[], metavar="KEY",
        help="key instalasi (mis. efi:fedora) atau ref komponen; boleh diulang",
    )
    backup = commands.add_parser("backup", help="preflight + backup per lapisan + verifikasi (tidak mengubah sistem)")
    target = backup.add_mutually_exclusive_group()
    target.add_argument(
        "--usb", "--drive", dest="usb", metavar="HURUF",
        help="huruf drive eksternal (flashdisk/HDD) tujuan salinan backup, mis. E",
    )
    target.add_argument(
        "--local-only",
        action="store_true",
        help="mode coba: simpan hanya di C:, tanpa USB (belum memenuhi syarat eksekusi M2b)",
    )
    verify = commands.add_parser("verify-backup", help="verifikasi ulang folder backup terhadap manifest-nya")
    verify.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroRelay\\backups\\20261021-193000")
    uninstall = commands.add_parser(
        "uninstall", help="JALANKAN rencana uninstall (menghapus entri boot & folder ESP), dengan banyak konfirmasi"
    )
    uninstall.add_argument("--select", action="append", default=[], metavar="KEY", help="sama seperti di `plan`")
    uninstall.add_argument("--backup", metavar="ID", help="ID backup yang sudah ada salinannya di drive eksternal")
    restore = commands.add_parser("restore", help="pulihkan folder ESP dan/atau entri boot dari backup")
    restore.add_argument("--backup", metavar="ID", required=True)
    restore.add_argument("--esp-item", action="append", default=[], metavar="PATH", help="mis. EFI\\fedora")
    restore.add_argument("--boot-entry", action="append", default=[], metavar="NAMA",
                         help="nama entri (mis. Fedora), atau nomornya (mis. Boot0007) kalau namanya dipakai lebih dari satu entri")
    commands.add_parser("journal", help="tampilkan sesi eksekusi, termasuk yang terputus di tengah")
    return parser


# Perintah yang hanya membaca file DistroRelay sendiri tidak perlu hak Administrator.
_NO_ADMIN_COMMANDS = {"verify-backup", "journal"}


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _pause() -> None:
    try:
        input("\nTekan Enter untuk menutup jendela ini…")
    except EOFError:
        pass


def _run_scan_command(args: argparse.Namespace, log_path: Path | None) -> int:
    from rich.console import Console

    from . import render
    from .windows.scanner import report_to_json, run_scan, save_raw

    console = Console()
    with console.status("Scanning…") as status:
        report = run_scan(progress=lambda message: status.update(message))
    console.print(render.to_rich_tree("DistroRelay: hasil scan (read-only)", render.summary_nodes(report)))
    console.print(render.info_text(report, str(log_path) if log_path else None))
    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(report_to_json(report), encoding="utf-8")
        console.print(f"Laporan JSON disimpan ke {json_path.resolve()}")
    if args.raw_dir:
        written = save_raw(report, Path(args.raw_dir))
        console.print(f"{len(written)} file output mentah disimpan ke {Path(args.raw_dir).resolve()}")
    return 0 if all(i.severity != "error" for i in report.issues) else 1


def _ask_word(prompt: str) -> str:
    """Untuk kata konfirmasi (HAPUS, LANJUT, PULIHKAN): tidak di-strip, supaya spasi di depan tetap ditolak."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def _scan_with_status(console, flow=None):
    from .windows.scanner import run_scan

    with console.status("Scanning…") as status:
        if flow is not None:
            report = flow.scan(progress=lambda message: status.update(message))
        else:
            report = run_scan(progress=lambda message: status.update(message))
    for issue in report.issues:
        if issue.severity != "info":
            console.print(f"Scan: [{issue.step}] {issue.message}", style="yellow", markup=False, highlight=False)
    return report


def _run_plan_command(args: argparse.Namespace) -> int:
    from rich.console import Console

    from . import render
    from .windows.uninstall_plan import build_plan, expand_selection

    console = Console()
    report = _scan_with_status(console)
    if report.analysis is None:
        console.print("Analisis tidak tersedia.")
        return 1
    if not args.select:
        console.print(render.candidates_text(report.analysis))
        return 0
    refs, unknown = expand_selection(report.analysis, args.select)
    for selector in unknown:
        console.print(f"Tidak dikenal: {selector}", style="red", markup=False)
    plan = build_plan(report.analysis, report.boot_entries, refs)
    console.print(render.plan_text(plan))
    return 1 if unknown else 0


def _run_backup_command(args: argparse.Namespace) -> int:
    from rich.console import Console

    from . import render
    from .windows import disks, esp
    from .windows.backup import BackupError, backup_runner, create_backup
    from .windows.preflight import run_preflight
    from .windows.runner import SubprocessRunner

    console = Console()
    report = _scan_with_status(console)
    runner = backup_runner(SubprocessRunner())
    with console.status("Preflight…"):
        fresh, _ = disks.load_inventory(runner)
        preflight = run_preflight(report, runner, fresh_inventory=fresh, mount_esp=esp.mounted_esp)
    console.print(render.preflight_text(preflight))
    if not (preflight.can_backup_locally if args.local_only else preflight.can_backup):
        console.print("Backup dibatalkan: ada syarat yang gagal (lihat di atas).", style="bold red")
        return 1

    letters = [t.drive for t in preflight.usb_targets]
    if args.local_only:
        wanted = None
    elif args.usb:
        wanted = args.usb.strip(":\\ ").upper()
        if wanted not in letters:
            console.print(
                f"Drive {wanted}: bukan drive eksternal, tidak ada, atau ruangnya kurang. "
                f"Pilihan: {', '.join(letters) or '-'}",
                style="red",
            )
            return 1
    elif len(letters) == 1:
        wanted = letters[0]
    else:
        console.print(f"Ada beberapa drive eksternal ({', '.join(letters)}). Pilih dengan --drive HURUF.", style="yellow")
        return 1

    try:
        with console.status("Backup…") as status:
            result = create_backup(
                report,
                runner,
                esp.mounted_esp,
                usb_root=Path(f"{wanted}:\\") if wanted else None,
                local_only=args.local_only,
                progress=lambda m: status.update(m),
            )
    except BackupError as exc:
        console.print(f"Backup gagal: {exc}", style="bold red", markup=False)
        return 1
    console.print(render.backup_text(result))
    return 0 if result.ok else 1


def _run_verify_command(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.text import Text

    from .windows.backup import verify_backup_dir

    console = Console()
    checks = verify_backup_dir(Path(args.directory))
    for check in checks:
        line = Text("[ok]    " if check.ok else "[GAGAL] ", style="green" if check.ok else "bold red")
        line.append(f"{check.name}: {check.message}")
        console.print(line)
    return 0 if all(c.ok for c in checks) else 1


def _run_journal_command(args: argparse.Namespace) -> int:
    from rich.console import Console

    from . import render
    from .windows.journal import sessions

    Console().print(render.journal_text(sessions()))
    return 0


def _run_uninstall_command(args: argparse.Namespace, flow=None) -> int:
    from rich.console import Console

    from . import render
    from .windows.stopping import StopSignal, console_stop_guard
    from .windows.workflow import CONTINUE_WORD, DELETE_WORD, UninstallFlow, default_deps

    console = Console()
    flow = flow or UninstallFlow(default_deps())
    unfinished = flow.unfinished_sessions()
    if unfinished:
        console.print(render.journal_text(unfinished))
        console.print(
            "Ada sesi sebelumnya yang belum tuntas (terputus, dihentikan, atau gagal), jadi sistem mungkin "
            "setengah jadi. Periksa dulu kondisinya (scan ulang).",
            style="bold yellow",
        )
        if not flow.acknowledge_unfinished(_ask_word(f"Tetap lanjut? Ketik {CONTINUE_WORD}: ")):
            return 1
    if not args.backup:
        available = sorted(p.name for p in flow.deps.backups_root.glob("*") if (p / "manifest.json").exists())
        console.print(f"Wajib --backup <ID>. Backup yang ada: {', '.join(available) or '-'}", style="red")
        return 1
    if not args.select:
        console.print("Wajib --select (lihat kandidat dengan `distrorelay plan`).", style="red")
        return 1

    # Syarat proses dari plan yang tidak bisa dicek kode: test VM dan latihan prosedur darurat.
    choice = _ask("Ini dijalankan di VM (untuk test) atau di laptop asli? Ketik vm atau asli: ").lower()
    if choice not in ("vm", "asli"):
        console.print("Dibatalkan.")
        return 1
    decision = flow.decide_mode(choice)
    if decision.forced_real:
        console.print(
            f"Komputer ini terdeteksi sebagai mesin fisik ({decision.machine.description}), bukan VM: "
            "diperlakukan sebagai laptop asli.",
            style="bold yellow",
            markup=False,
        )
    answers = {}
    for key, question in decision.statements.items():
        answers[key] = _ask(f"{question} Ketik ya: ").lower() == "ya"
        if not answers[key]:
            break
    if flow.set_mode(choice, answers):
        console.print("Selesaikan dulu di VM. Tidak ada yang diubah.", style="yellow")
        return 1

    report = _scan_with_status(console, flow)
    plan = flow.select(args.select)
    if flow.unknown:
        console.print(f"Tidak dikenal: {', '.join(flow.unknown)}", style="red", markup=False)
        return 1
    console.print(render.plan_text(plan))

    with console.status("Preflight…"):
        preflight = flow.run_preflight()
    console.print(render.preflight_text(preflight))
    for check in preflight.needs_confirmation:
        console.print(f"\n{check.title}: {check.message}", style="bold yellow", markup=False)
        flow.confirm(check.key, _ask("Sudah? Ketik ya: ").lower() == "ya")

    flow.use_backup(args.backup)
    with console.status("Memverifikasi backup lokal & eksternal…"):
        gate = flow.gate()
    console.print(render.gate_text(gate))
    if not gate.ok:
        return 1

    console.print("\nIni akan MENGHAPUS entri boot dan folder ESP sesuai rencana di atas.", style="bold red")
    ticket = flow.authorize(_ask_word(f"Ketik {DELETE_WORD} untuk menjalankan, atau Enter untuk batal: "))
    if ticket is None:
        console.print("Dibatalkan. Tidak ada yang diubah.")
        return 1

    stop = StopSignal()
    with console.status("Menjalankan… (Ctrl+C = berhenti setelah langkah yang sedang jalan)") as status, \
            console_stop_guard(stop, notify=lambda m: status.update(m)):
        result = flow.execute(ticket, progress=lambda m: status.update(m), stop=stop)
    console.print(render.execution_text(result))
    if result.stopped_by_user:
        console.print(
            "\nDihentikan dengan rapi: langkah yang sudah jalan tetap berlaku, sisanya belum dijalankan, dan Windows "
            "tetap bisa boot. Untuk melanjutkan, buat backup baru lalu jalankan uninstall lagi; untuk membatalkan "
            f"yang sudah dihapus, pakai `distrorelay restore --backup {args.backup} ...`.",
            style="bold yellow",
        )
        return 1

    with console.status("Post-check (scan ulang)…") as status:
        checks = flow.post_check(progress=lambda m: status.update(m))
    console.print(render.verifications_text("Post-check", checks))
    if not result.completed or not all(c.ok for c in checks):
        console.print(
            f"\nAda yang tidak sesuai. Pulihkan dengan `distrorelay restore --backup {args.backup} ...`, "
            "atau ikuti docs/RECOVERY.md kalau Windows tidak bisa boot.",
            style="bold yellow",
        )
        return 1
    return 0


def _run_restore_command(args: argparse.Namespace) -> int:
    import json

    from rich.console import Console

    from . import render
    from .windows import disks, esp, flush
    from .windows.backup import Verification, backups_root, verify_backup_dir
    from .windows.restore import (
        describe_entry,
        restore_boot_entry,
        restore_esp_items,
        restore_runner,
        select_backup_entries,
    )

    def _durable(label, action) -> Verification:
        try:
            action()
        except flush.FlushError as exc:
            return Verification(label, False, str(exc))
        return Verification(label, True, "Ya")
    from .windows.runner import SubprocessRunner

    console = Console()
    backup = backups_root() / args.backup
    failed = [v for v in verify_backup_dir(backup) if not v.ok]
    if failed:
        console.print(render.verifications_text("Backup tidak valid", failed))
        return 1
    records = json.loads((backup / "firmware-entries.json").read_text(encoding="utf-8"))
    if not args.esp_item and not args.boot_entry:
        console.print("Pilih yang mau dipulihkan dengan --esp-item PATH dan/atau --boot-entry NAMA atau Boot####.")
        console.print("Entri di backup: " + ", ".join(describe_entry(r) for r in records), markup=False)
        return 1
    chosen, problem = select_backup_entries(records, args.boot_entry)
    if problem:
        console.print(problem, style="red", markup=False)
        return 1
    console.print(f"Akan dipulihkan: folder/file {args.esp_item or '-'}, entri {[r['description'] for r in chosen] or '-'}",
                  markup=False)
    from .windows.workflow import RESTORE_WORD, typed

    if not typed(_ask_word(f"Ketik {RESTORE_WORD} untuk menjalankan: "), RESTORE_WORD):
        console.print("Dibatalkan.")
        return 1

    runner, created = restore_runner(SubprocessRunner())
    inventory, _ = disks.load_inventory(runner)
    results = []
    if args.esp_item:
        with esp.mounted_esp(runner, inventory.esp()) as mounted:
            results += restore_esp_items(backup, args.esp_item, mounted.root)
            results.append(_durable("Folder/file ESP ditulis ke disk", lambda: flush.flush_volume(mounted.root)))
    for record in chosen:
        results.append(restore_boot_entry(runner, created, record))
    if chosen:
        results.append(_durable("BCD ditulis ke disk", flush.flush_bcd))
    console.print(render.verifications_text("Pemulihan", results))
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.verbose)
    # Konsol Windows asli sudah UTF-8 (PEP 528); output yang di-redirect (file, pipe, NUL) belum tentu.
    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")

    if sys.platform != "win32":
        print("Bagian Windows DistroRelay hanya berjalan di Windows.")
        return 2

    from .windows.elevate import is_admin, relaunch_as_admin

    if not is_admin() and not args.no_elevate and args.command not in _NO_ADMIN_COMMANDS:
        print("DistroRelay butuh hak Administrator untuk membaca ESP, NVRAM, dan header partisi.")
        print("Membuka jendela Administrator (konfirmasi di dialog UAC)…")
        if relaunch_as_admin(["--elevated-child", *argv]):
            return 0
        print("Permintaan Administrator dibatalkan.")
        print("Untuk scan terbatas tanpa admin: python -m distrorelay --no-elevate scan")
        return 1

    try:
        if args.command == "scan":
            code = _run_scan_command(args, log_path)
        elif args.command == "plan":
            code = _run_plan_command(args)
        elif args.command == "backup":
            code = _run_backup_command(args)
        elif args.command == "verify-backup":
            code = _run_verify_command(args)
        elif args.command == "uninstall":
            code = _run_uninstall_command(args)
        elif args.command == "restore":
            code = _run_restore_command(args)
        elif args.command == "journal":
            code = _run_journal_command(args)
        else:
            from .tui.app import run_tui

            code = run_tui(str(log_path) if log_path else None)
    except KeyboardInterrupt:
        code = 130
    except Exception:
        log.exception("Error tidak terduga")
        traceback.print_exc()
        code = 1
        if args.elevated_child:
            _pause()
        return code
    if args.elevated_child and args.command in ("scan", "plan", "backup", "uninstall", "restore"):
        _pause()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
