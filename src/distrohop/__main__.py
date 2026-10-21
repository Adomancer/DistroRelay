"""Entry point: `python -m distrohop [scan]`."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from . import __version__
from .logging_setup import setup_logging

log = logging.getLogger("distrohop")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distrohop",
        description=(
            "Toolkit distro-hopping untuk dual-boot Windows + Linux. "
            "Versi ini belum mengubah apa pun: scan, backup, dan dry-run rencana uninstall."
        ),
    )
    parser.add_argument("--version", action="version", version=f"DistroHop {__version__}")
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
    verify.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroHop\\backups\\20261021-193000")
    return parser


# Perintah yang hanya membaca file backup tidak perlu hak Administrator.
_NO_ADMIN_COMMANDS = {"verify-backup"}


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
    console.print(render.to_rich_tree("DistroHop: hasil scan (read-only)", render.summary_nodes(report)))
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


def _scan_with_status(console):
    from .windows.scanner import run_scan

    with console.status("Scanning…") as status:
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    log_path = setup_logging(args.verbose)
    # Konsol Windows asli sudah UTF-8 (PEP 528); output yang di-redirect (file, pipe, NUL) belum tentu.
    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")

    if sys.platform != "win32":
        print("Bagian Windows DistroHop hanya berjalan di Windows.")
        return 2

    from .windows.elevate import is_admin, relaunch_as_admin

    if not is_admin() and not args.no_elevate and args.command not in _NO_ADMIN_COMMANDS:
        print("DistroHop butuh hak Administrator untuk membaca ESP, NVRAM, dan header partisi.")
        print("Membuka jendela Administrator (konfirmasi di dialog UAC)…")
        if relaunch_as_admin(["--elevated-child", *argv]):
            return 0
        print("Permintaan Administrator dibatalkan.")
        print("Untuk scan terbatas tanpa admin: python -m distrohop --no-elevate scan")
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
    if args.elevated_child and args.command in ("scan", "plan", "backup"):
        _pause()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
