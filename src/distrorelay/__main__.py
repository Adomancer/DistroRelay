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
        help="key instalasi (mis. efi:fedora, termasuk partisinya) atau ref komponen; boleh diulang",
    )
    plan.add_argument("--extend-c", action="store_true",
                      help="perluas C: ke ruang bekas partisi (hanya kalau partisinya tepat sesudah C:)")
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
    uninstall.add_argument("--extend-c", action="store_true", help="sama seperti di `plan`")
    restore = commands.add_parser("restore", help="pulihkan partisi Linux, folder ESP, dan/atau entri boot dari backup")
    restore.add_argument("--backup", metavar="ID", required=True)
    restore.add_argument("--esp-item", action="append", default=[], metavar="PATH", help="mis. EFI\\fedora")
    restore.add_argument("--boot-entry", action="append", default=[], metavar="NAMA",
                         help="nama entri (mis. Fedora), atau nomornya (mis. Boot0007) kalau namanya dipakai lebih dari satu entri")
    restore.add_argument("--partition", action="append", default=[], metavar="ID",
                         help="partisi Linux dari backup (mis. disk0-p5 atau GUID partisinya)")
    commands.add_parser("journal", help="tampilkan sesi eksekusi, termasuk yang terputus di tengah")

    profile = commands.add_parser("profile", help="profil distro-hopping (aplikasi, repo, dotfiles) untuk Linux kit")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    init = profile_commands.add_parser("init", help="buat profile.toml awal untuk diedit")
    init.add_argument("--force", action="store_true", help="timpa profile.toml yang sudah ada")
    profile_commands.add_parser("show", help="cek profil dan tampilkan ringkasannya")
    profile_import = profile_commands.add_parser("import", help="gabungkan hasil `backup` dari Linux ke profil")
    profile_import.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroRelay\\linux-backups\\<id>")
    profile_import.add_argument("--yes", action="store_true", help="tanpa konfirmasi")

    kit = commands.add_parser("kit", help="Linux kit: export ke USB / partisi NTFS, verifikasi")
    kit_commands = kit.add_subparsers(dest="kit_command", required=True)
    export = kit_commands.add_parser("export", help="tulis kit + profil ke <drive>:\\DistroRelay\\kit")
    export.add_argument("--drive", metavar="HURUF", help="drive tujuan, mis. E (flashdisk)")
    export.add_argument("--ntfs", action="store_true", help="juga ke C:\\DistroRelay\\kit (partisi Windows)")
    kit_verify = kit_commands.add_parser("verify", help="cek ulang hash semua file kit")
    kit_verify.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroRelay\\kit")

    fast = commands.add_parser("fast-startup", help="lihat / ubah Fast Startup (harus mati supaya Linux bisa menulis ke NTFS)")
    switch = fast.add_mutually_exclusive_group()
    switch.add_argument("--off", action="store_true", help="matikan Fast Startup")
    switch.add_argument("--on", action="store_true", help="nyalakan lagi Fast Startup")

    iso = commands.add_parser("iso", help="ISO manager: cek versi terbaru, unduh + verifikasi GPG/SHA256, salin ke Ventoy")
    iso_commands = iso.add_subparsers(dest="iso_command", required=True)
    iso_check = iso_commands.add_parser("check", help="versi terbaru tiap distro vs ISO di USB Ventoy")
    iso_check.add_argument("--drive", metavar="HURUF", help="USB Ventoy (default: dicari dari label 'Ventoy')")
    iso_download = iso_commands.add_parser("download", help="unduh ISO terbaru lalu verifikasi (bisa dilanjutkan)")
    iso_download.add_argument("distro", choices=ISO_DISTROS)
    iso_download.add_argument("--edition", help="mis. KDE (Fedora) atau xfce (Mint)")
    iso_verify = iso_commands.add_parser("verify", help="verifikasi ulang ISO yang sudah diunduh (tanpa internet)")
    iso_verify.add_argument("distro", choices=ISO_DISTROS)
    iso_verify.add_argument("--version", dest="iso_version", metavar="VERSI", help="default: unduhan terbaru")
    iso_verify.add_argument("--edition", help="kalau ada beberapa edisi yang diunduh")
    iso_copy = iso_commands.add_parser("copy", help="verifikasi ulang lalu salin ISO ke USB Ventoy")
    iso_copy.add_argument("distro", choices=ISO_DISTROS)
    iso_copy.add_argument("--version", dest="iso_version", metavar="VERSI", help="default: unduhan terbaru")
    iso_copy.add_argument("--edition", help="kalau ada beberapa edisi yang diunduh")
    iso_copy.add_argument("--drive", metavar="HURUF", help="USB Ventoy (default: dicari dari label 'Ventoy')")
    iso_commands.add_parser("keys", help="kunci GPG bawaan per distro dan lokasi gpgv")

    shrink = commands.add_parser(
        "shrink-c", help="kecilkan C: untuk ruang distro baru (MENGUBAH partisi, coba di VM dulu)"
    )
    shrink.add_argument("--size", type=float, metavar="GB", help="berapa GB dibebaskan; tanpa ini hanya menampilkan info")
    return parser


ISO_DISTROS = ("fedora", "arch", "ubuntu", "mint")


# Perintah yang hanya membaca file DistroRelay sendiri tidak perlu hak Administrator.
_NO_ADMIN_COMMANDS = {"verify-backup", "journal", "profile", "kit", "iso"}


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
    plan = build_plan(report.analysis, report.boot_entries, refs, inventory=report.inventory, fsinfo=report.fsinfo,
                      extend_c=args.extend_c)
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
    from .windows.workflow import CONTINUE_WORD, DELETE_WORD, UninstallFlow, default_deps, typed

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
    plan = flow.select(args.select, extend_c=getattr(args, "extend_c", False))
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
    if not sys.stdin.isatty():
        # Uji VM 2026-10-22: PowerShell mematikan program di dalam pipeline saat Ctrl+C, di tengah langkah.
        console.print(
            "Peringatan: input datang dari pipe/script. Di PowerShell, Ctrl+C pada pipeline mematikan proses ini "
            "di tengah langkah, jadi tidak bisa berhenti dengan rapi. Sebaiknya jalankan perintah ini langsung "
            "di jendela PowerShell/CMD.",
            style="yellow",
        )
    typed_words = {}
    for required in flow.required_typing():
        console.print(f"\n{required.prompt}", style="bold yellow", markup=False)
        typed_words[required.key] = _ask_word(f"{required.title} (ketik {required.word}): ")
        if not typed(typed_words[required.key], required.word):
            console.print("Tidak cocok. Dibatalkan, tidak ada yang diubah.")
            return 1
    ticket = flow.authorize(_ask_word(f"Ketik {DELETE_WORD} untuk menjalankan, atau Enter untuk batal: "), typed_words)
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


def _run_restore_command(args: argparse.Namespace, flow=None) -> int:
    from rich.console import Console

    from . import render
    from .windows.workflow import RESTORE_WORD, RestoreFlow, default_deps

    console = Console()
    flow = flow or RestoreFlow(default_deps())
    with console.status("Memverifikasi backup…"):
        failed = [v for v in flow.load(args.backup) if not v.ok]
    if failed:
        console.print(render.verifications_text("Backup tidak valid", failed))
        return 1
    partitions = getattr(args, "partition", [])
    if not args.esp_item and not args.boot_entry and not partitions:
        console.print("Pilih yang mau dipulihkan dengan --partition ID, --esp-item PATH, dan/atau --boot-entry NAMA atau Boot####.")
        for choice in flow.partition_choices():
            note = f"  ({choice.blocked})" if choice.blocked else ""
            console.print(f"Partisi di backup: {choice.label}{note}", markup=False)
        console.print("Folder/file di backup: " + ", ".join(c.key for c in flow.esp_choices()), markup=False)
        console.print("Entri di backup: " + ", ".join(c.label for c in flow.entry_choices()), markup=False)
        return 1
    problem = flow.select(args.esp_item, args.boot_entry, partitions)
    if problem:
        console.print(problem, style="red", markup=False)
        return 1
    console.print(flow.summary(), markup=False)
    ticket = flow.authorize(_ask_word(f"Ketik {RESTORE_WORD} untuk menjalankan: "))
    if ticket is None:
        console.print("Dibatalkan.")
        return 1
    with console.status("Memulihkan…") as status:
        results = flow.run(ticket, progress=lambda m: status.update(m))
    console.print(render.verifications_text("Pemulihan", results))
    return 0 if all(r.ok for r in results) else 1


def _run_profile_command(args: argparse.Namespace) -> int:
    from rich.console import Console

    from . import render
    from .common import profile as profile_mod
    from .windows import kit_export

    console = Console()
    path = kit_export.profile_path()
    if args.profile_command == "init":
        if path.exists() and not args.force:
            console.print(f"{path} sudah ada. Edit langsung, atau pakai --force untuk menimpanya.", markup=False)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile_mod.to_toml(profile_mod.template()), encoding="utf-8")
        console.print(f"Profil awal ditulis ke {path}. Edit, lalu cek dengan `distrorelay profile show`.", markup=False)
        return 0
    try:
        profile = kit_export.load_profile(path)
        package_map = kit_export.load_package_map()
    except (profile_mod.ProfileError, ValueError) as exc:
        console.print(f"Profil di {path} tidak valid:", style="bold red", markup=False)
        for problem in getattr(exc, "problems", [str(exc)]):
            console.print(f"  - {problem}", markup=False)
        return 1
    if args.profile_command == "import":
        return _import_linux_backup(console, profile, path, Path(args.directory), args.yes)
    console.print(render.profile_text(profile, package_map, path))
    return 0


def _import_linux_backup(console, profile, path: Path, folder: Path, yes: bool) -> int:
    from datetime import datetime

    from distrorelay_linux import backup as linux_backup

    from .common import profile as profile_mod

    problems = linux_backup.verify(folder)
    if problems:
        console.print(f"Backup {folder} tidak bisa dipakai: {'; '.join(problems)}", style="bold red", markup=False)
        return 1
    manifest = linux_backup.read_manifest(folder)
    merged, changes = profile_mod.merge_update(profile, manifest.get("profile_update") or {})
    distro = (manifest.get("distro") or {}).get("pretty_name", "?")
    console.print(f"Backup {manifest.get('backup_id')} dari {distro}", markup=False)
    if not changes:
        console.print("Profil sudah memuat semua isi backup ini; tidak ada yang berubah.")
        return 0
    for change in changes:
        console.print(f"  {change}", markup=False)
    if not yes and _ask("Tambahkan ke profil? Ketik ya: ").lower() != "ya":
        console.print("Dibatalkan, profil tidak berubah.")
        return 1
    backup_path = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    backup_path.write_bytes(path.read_bytes())
    path.write_text(profile_mod.to_toml(merged), encoding="utf-8")
    console.print(f"Profil diperbarui ({path}); versi lama (termasuk komentarnya) di {backup_path}. "
                  "Export ulang kit supaya ikut ke Linux.", markup=False)
    return 0


def _run_kit_command(args: argparse.Namespace) -> int:
    from rich.console import Console

    from . import render
    from .common import profile as profile_mod
    from .windows import kit_export

    console = Console()
    if args.kit_command == "verify":
        from distrorelay_linux import kit as linux_kit

        problems = linux_kit.verify(Path(args.directory))
        for problem in problems:
            console.print(f"[GAGAL] {problem}", style="bold red", markup=False)
        if not problems:
            console.print("Semua file kit cocok dengan manifest.", style="green")
        return 1 if problems else 0

    roots = []
    if args.drive:
        letter = args.drive.strip(":\\ ").upper()
        if len(letter) != 1 or not letter.isalpha() or not Path(f"{letter}:\\").exists():
            console.print(f"Drive {args.drive} tidak ada.", style="red", markup=False)
            return 1
        roots.append(Path(f"{letter}:\\"))
    if args.ntfs and Path("C:\\") not in roots:
        roots.append(Path("C:\\"))
    if not roots:
        console.print("Pilih tujuan: --drive HURUF (flashdisk) dan/atau --ntfs (C:\\DistroRelay\\kit).")
        return 1
    try:
        profile = kit_export.load_profile()
        package_map = kit_export.load_package_map()
    except (profile_mod.ProfileError, ValueError) as exc:
        console.print(f"Profil tidak valid: {exc}", style="bold red", markup=False)
        return 1
    code = 0
    for root in roots:
        try:
            result = kit_export.export_kit(profile, package_map, root)
        except (OSError, kit_export.KitExportError) as exc:
            console.print(f"Export ke {root} gagal: {exc}", style="bold red", markup=False)
            code = 1
            continue
        console.print(render.kit_export_text(result))
        code = code or (0 if result.ok else 1)
    if Path("C:\\") in roots and kit_export.fast_startup_enabled():
        console.print(
            "Fast Startup masih AKTIF: Linux hanya bisa me-mount partisi Windows read-only, jadi kit di C: bisa dipakai "
            "untuk setup, tapi backup dari Linux tidak bisa disimpan di sana. Matikan dengan "
            "`distrorelay fast-startup --off`.",
            style="yellow",
        )
    return code


def _run_fast_startup_command(args: argparse.Namespace) -> int:
    from .windows import kit_export

    if args.off or args.on:
        try:
            kit_export.set_fast_startup(bool(args.on))
        except OSError as exc:
            print(f"Gagal mengubah Fast Startup: {exc}")
            return 1
        print(f"Fast Startup sekarang {'AKTIF' if args.on else 'MATI'} (berlaku mulai shutdown berikutnya).")
        return 0
    state = kit_export.fast_startup_enabled()
    print({True: "Fast Startup AKTIF.", False: "Fast Startup MATI.", None: "Status Fast Startup tidak terbaca."}[state])
    return 0


def _pick_drive(letter: str | None, volumes, *, required: bool):
    """(volume, catatan). Tanpa --drive: satu-satunya USB berlabel Ventoy. Volume None = tidak ada / ditolak."""
    from .windows.iso import catalog

    found = volumes()
    if letter:
        wanted = letter.strip(":\\ ").upper()
        volume = next((v for v in found if v.letter == wanted), None)
        if volume is None:
            return None, f"Drive {wanted}: tidak ada."
        if volume.is_system:
            return None, f"{wanted}: adalah drive Windows, bukan USB Ventoy."
        note = None if catalog.is_ventoy(volume) else (
            f"{wanted}: tidak terlihat seperti USB Ventoy (label '{volume.label}', tanpa folder ventoy); tetap dipakai.")
        return volume, note
    ventoys = catalog.ventoy_volumes(found)
    if len(ventoys) == 1:
        return ventoys[0], None
    if ventoys:
        return None, f"Ada beberapa USB Ventoy ({', '.join(v.letter for v in ventoys)}); pilih dengan --drive HURUF."
    return None, ("USB Ventoy tidak terdeteksi (label 'Ventoy'). Colokkan, atau pilih dengan --drive HURUF."
                  if required else "USB Ventoy tidak terdeteksi, jadi hanya versi terbaru yang ditampilkan.")


def _byte_progress(console):
    from rich.progress import (BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn,
                               TransferSpeedColumn)

    return Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(),
                    TimeRemainingColumn(), console=console, transient=True)


def _progress_callback(progress, description: str):
    task = progress.add_task(description, total=None)

    def update(done: int, total: int | None) -> None:
        progress.update(task, completed=done, total=total)

    return update


def _run_iso_command(args: argparse.Namespace, tools=None, volumes=None) -> int:
    from rich.console import Console

    from . import render
    from .format import human_size
    from .windows.iso import IsoError, catalog, manager, saved_releases, sources, verify
    from .windows.iso.download import DownloadStopped, fetch

    console = Console()
    volumes = volumes or catalog.list_volumes
    if args.iso_command == "keys":
        console.print(render.iso_keys_text(sources.SOURCES, tools.gpgv if tools else verify.find_gpgv(),
                                           verify.INSTALL_HINT))
        return 0
    if args.iso_command == "check":
        drive, note = _pick_drive(args.drive, volumes, required=False)
        if drive is None and args.drive:
            console.print(note, style="red", markup=False)
            return 1
        with console.status("Mengecek versi terbaru…"):
            rows = catalog.check(sources.SOURCES, tools.fetch if tools else fetch,
                                 drive=drive.root if drive else None, downloads_root=tools.root if tools else None)
        console.print(render.iso_catalog_text(rows, drive, note))
        return 0 if all(row.latest is not None for row in rows) else 1

    gpgv = tools.gpgv if tools else verify.find_gpgv()
    if not gpgv:
        console.print(f"gpgv (GnuPG) tidak ditemukan, jadi ISO tidak bisa diverifikasi. Pasang dengan "
                      f"`{verify.INSTALL_HINT}`, lalu buka terminal baru.", style="bold red", markup=False)
        return 1
    tools = tools or manager.Tools(gpgv=gpgv)
    source = sources.get(args.distro)

    if args.iso_command == "download":
        try:
            with console.status(f"Mengecek versi terbaru {source.NAME}…"):
                release = source.latest(tools.fetch, args.edition)
            problems = release.problems()
            if problems:
                raise IsoError("; ".join(problems))
            size = f", {human_size(release.size)}" if release.size else ""
            console.print(f"Terbaru: {source.NAME} {release.version} ({release.edition}{size})", markup=False)
            with _byte_progress(console) as progress:
                result = manager.prepare(
                    release, source.KEYS, tools,
                    progress=_progress_callback(progress, "Mengunduh"),
                    hash_progress=_progress_callback(progress, "Menghitung SHA256"),
                )
        except (DownloadStopped, KeyboardInterrupt):
            console.print("Unduhan dihentikan. Jalankan perintah yang sama lagi untuk melanjutkan.", style="yellow")
            return 130
        except IsoError as exc:
            console.print(f"Gagal: {exc}", style="bold red", markup=False)
            return 1
        console.print(render.verifications_text("Verifikasi", result.verification.steps))
        for note in result.notes:
            console.print(f"  {note}", style="dim", markup=False)
        if not result.ok:
            console.print("ISO TIDAK lolos verifikasi; jangan dipakai.", style="bold red")
            return 1
        console.print(f"ISO siap di {result.folder}. Salin ke USB Ventoy dengan `distrorelay iso copy {args.distro}`.",
                      style="green", markup=False)
        return 0

    candidates = [s for s in saved_releases(args.distro, tools.root)
                  if (not args.iso_version or s.release.version == args.iso_version)
                  and (not args.edition or s.release.edition.lower() == args.edition.lower())]
    if not candidates:
        version = f" versi {args.iso_version}" if args.iso_version else ""
        console.print(f"Belum ada unduhan {source.NAME}{version}. "
                      f"Unduh dulu dengan `distrorelay iso download {args.distro}`.", markup=False)
        return 1
    saved = candidates[0]
    console.print(f"{source.NAME} {saved.release.version} ({saved.release.edition}): {saved.iso_path}", markup=False)

    if args.iso_command == "verify":
        with _byte_progress(console) as progress:
            result = manager.reverify(saved, source.KEYS, tools,
                                      hash_progress=_progress_callback(progress, "Menghitung SHA256"))
        console.print(render.verifications_text("Verifikasi ulang", result.steps))
        return 0 if result.ok else 1

    drive, note = _pick_drive(args.drive, volumes, required=True)
    if drive is None:
        console.print(note, style="red", markup=False)
        return 1
    if note:
        console.print(note, style="yellow", markup=False)
    with _byte_progress(console) as progress:
        steps = manager.copy_to_drive(
            saved, source.KEYS, tools, drive.root, filesystem=drive.filesystem,
            progress=_progress_callback(progress, f"Menyalin ke {drive.letter}:"),
            hash_progress=_progress_callback(progress, "Membaca ulang"),
        )
    console.print(render.verifications_text(f"Salin ke USB {drive.letter}:", steps))
    if not all(step.ok for step in steps):
        return 1
    older = [iso for iso in catalog.scan_isos(drive.root, {args.distro: source})
             if iso.path.name != saved.release.iso_name]
    if older:
        console.print("ISO lama yang masih ada di USB (tidak dihapus otomatis): "
                      + ", ".join(iso.path.name for iso in older), style="dim", markup=False)
    return 0


def _run_shrink_command(args: argparse.Namespace, inner=None, journal_root: Path | None = None) -> int:
    from rich.console import Console

    from . import render
    from .format import human_size
    from .windows import shrink
    from .windows.journal import incomplete_sessions
    from .windows.runner import ReadOnlyRunner, SubprocessRunner
    from .windows.workflow import CONTINUE_WORD, typed

    console = Console()
    inner = inner or SubprocessRunner()
    try:
        with console.status("Membaca partisi C: (menghitung batas shrink bisa ±1 menit)…"):
            drive = shrink.read_c_drive(ReadOnlyRunner(inner))
    except shrink.ShrinkError as exc:
        console.print(str(exc), style="bold red", markup=False)
        return 1
    console.print(
        f"C: {human_size(drive.size)}, kosong {human_size(drive.free)}. Bisa dibebaskan maksimal "
        f"{human_size(drive.max_shrink)} (batas Windows {human_size(drive.size - drive.size_min)}, "
        f"dan C: tetap kosong ≥ {human_size(shrink.MIN_FREE_AFTER)}).",
        markup=False,
    )
    if args.size is None:
        console.print("Pakai --size GB untuk mengecilkan C:.", style="dim")
        return 0
    plan = shrink.plan_shrink(drive, int(args.size * shrink.GIB))
    for line in plan.summary():
        console.print(f"  {line}", markup=False)
    if not plan.ok:
        for problem in plan.problems:
            console.print(f"  [GAGAL] {problem}", style="bold red", markup=False)
        return 1
    unfinished = incomplete_sessions(journal_root)
    if unfinished:
        console.print(render.journal_text(unfinished))
        if not typed(_ask_word(f"Ada sesi yang belum tuntas. Tetap lanjut? Ketik {CONTINUE_WORD}: "), CONTINUE_WORD):
            return 1
    console.print(
        "\nIni MENGUBAH tabel partisi (sama seperti Shrink Volume di Disk Management). Colokkan charger dan jangan "
        "matikan laptop selama proses. Sebaiknya dicoba di VM dulu.",
        style="bold yellow",
    )
    if not typed(_ask_word(f"Ketik {shrink.SHRINK_WORD} untuk menjalankan, atau Enter untuk batal: "), shrink.SHRINK_WORD):
        console.print("Dibatalkan. Tidak ada yang diubah.")
        return 1
    with console.status("Mengecilkan C: …"):
        result = shrink.execute(plan, inner, journal_root=journal_root)
    console.print(result.message, style="green" if result.ok else "bold red", markup=False)
    return 0 if result.ok else 1


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
        elif args.command == "profile":
            code = _run_profile_command(args)
        elif args.command == "kit":
            code = _run_kit_command(args)
        elif args.command == "fast-startup":
            code = _run_fast_startup_command(args)
        elif args.command == "iso":
            code = _run_iso_command(args)
        elif args.command == "shrink-c":
            code = _run_shrink_command(args)
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
    if args.elevated_child and args.command in ("scan", "plan", "backup", "uninstall", "restore", "fast-startup",
                                                "shrink-c"):
        _pause()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
