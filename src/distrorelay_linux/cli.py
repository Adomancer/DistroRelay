"""CLI Linux kit: `info`, `kits`, `setup` (M4), `backup`, `backups` (M5)."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from . import backup as backup_mod
from . import distro as distro_mod
from . import kit as kit_mod
from . import provision
from .runner import SubprocessRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distrorelay-linux",
        description="DistroRelay Linux kit: pasang ulang aplikasi & dotfiles dari profil (jalankan lewat bootstrap.sh)",
    )
    parser.add_argument("--kit", metavar="DIR", help="folder kit (default: folder tempat kit ini dijalankan)")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("info", help="distro yang terdeteksi + isi kit (tidak mengubah apa pun)")
    commands.add_parser("kits", help="cari kit DistroRelay di drive yang ter-mount")
    setup = commands.add_parser("setup", help="pasang repo, paket, Flatpak, AUR, dan dotfiles dari profil")
    setup.add_argument("--dry-run", action="store_true", help="hanya tampilkan rencana dan perintahnya")
    setup.add_argument("--yes", action="store_true", help="jangan tanya lagi (termasuk membangun paru dari AUR)")
    setup.add_argument("--backup", metavar="DIR|latest|none", default="latest",
                       help="backup untuk dotfiles & file sensitif (default: backup terbaru di kit ini)")
    backup = commands.add_parser("backup", help="backup daftar paket + dotfiles (+ file sensitif terenkripsi) distro ini")
    backup.add_argument("--yes", action="store_true", help="tanpa checklist kurasi (semua paket yang lolos filter dibawa)")
    backup.add_argument("--no-sensitive", action="store_true", help="jangan backup dotfiles.sensitive")
    commands.add_parser("backups", help="daftar backup di kit ini + hasil verifikasinya")
    return parser


def _columns(names: list[str], width: int = 3) -> str:
    cells = [f"{n:>4}. {name}" for n, name in enumerate(names, 1)]
    size = max((len(c) for c in cells), default=0) + 2
    return "\n".join("".join(c.ljust(size) for c in cells[i:i + width]) for i in range(0, len(cells), width))


def _curate(candidates: list[str]) -> list[str]:
    """Checklist kurasi di terminal. Hasilnya nama yang TIDAK dibawa (disimpan ke profil lewat `profile import`)."""
    if not candidates:
        return []
    print(f"\nPaket yang akan dibawa ({len(candidates)}); paket sistem bawaan distro sudah disaring:\n")
    print(_columns(candidates))
    while True:
        try:
            answer = input("\nEnter = bawa semua; atau ketik nomor yang TIDAK dibawa (mis. 3 5 8-10): ")
        except EOFError:
            answer = ""
        chosen = backup_mod.parse_selection(answer, len(candidates))
        if chosen is not None:
            return [candidates[n - 1] for n in sorted(chosen)]
        print("Nomor tidak valid.")


def _usb_backup_dirs(runner) -> list[Path]:
    """Folder linux-backups milik kit lain yang ada di USB (tempat file sensitif kalau kit ini di NTFS)."""
    kits = [path for _, path in kit_mod.find_kits(kit_mod.default_search_roots())]
    return [path.parent / "linux-backups" for path in kits if backup_mod.on_usb(runner, path.parent)]


def _ask(question: str) -> bool:
    try:
        return input(f"{question} Ketik ya: ").strip().lower() == "ya"
    except EOFError:
        return False


def _kit_root(args) -> Path | None:
    if args.kit:
        return Path(args.kit)
    here = kit_mod.here()
    return here if (here / kit_mod.MANIFEST).is_file() else None


def _load_kit(args):
    root = _kit_root(args)
    if root is None:
        print("Folder kit tidak ditemukan. Jalankan lewat bootstrap.sh di folder kit, atau pakai --kit DIR.")
        return None
    try:
        return kit_mod.load(root)
    except kit_mod.KitError as exc:
        print(f"Kit di {root} tidak bisa dipakai:")
        for problem in exc.problems:
            print(f"  - {problem}")
        return None


def _newer_kit_note(kit) -> str | None:
    found = kit_mod.find_kits(kit_mod.default_search_roots())
    newer = [(created, path) for created, path in found if created > kit.created_at and path != kit.root.resolve()]
    if newer:
        created, path = newer[0]
        return f"Ada kit yang lebih baru di {path} (dibuat {created}); kit ini dibuat {kit.created_at}"
    return None


def cmd_info(args) -> int:
    distro = distro_mod.read()
    print(f"Distro: {distro.describe}")
    kit = _load_kit(args)
    if kit is None:
        return 1
    profile = kit.profile
    print(f"Kit: {kit.root} (dibuat {kit.created_at}, profil '{profile.name}', semua hash cocok)")
    print(f"  Paket: {len(profile.native)} nama logis, {len(profile.flatpak)} Flatpak, {len(profile.aur)} AUR")
    print(f"  Repo: {', '.join(profile.repos) or '-'}; dotfiles: {len(profile.dotfiles)} path")
    note = _newer_kit_note(kit)
    if note:
        print(note)
    return 0


def cmd_kits(args) -> int:
    found = kit_mod.find_kits(kit_mod.default_search_roots())
    if not found:
        print("Tidak ada kit DistroRelay di drive yang ter-mount.")
        return 1
    for created, path in found:
        print(f"{created}  {path}")
    return 0


def cmd_setup(args) -> int:
    kit = _load_kit(args)
    if kit is None:
        return 1
    distro = distro_mod.read()
    runner = SubprocessRunner()
    is_root = os.geteuid() == 0
    backup_dir, sensitive_file = None, None
    if args.backup == "latest":
        backup_dir = backup_mod.latest_backup(kit.backups_dir)
    elif args.backup != "none":
        backup_dir = Path(args.backup)
        problems = backup_mod.verify(backup_dir)
        if problems:
            print(f"Backup {backup_dir} tidak bisa dipakai: {'; '.join(problems)}")
            return 1
    if backup_dir is not None:
        print(f"Memakai backup {backup_dir}")
        search = [path.parent / "linux-backups" for _, path in kit_mod.find_kits(kit_mod.default_search_roots())]
        sensitive_file = backup_mod.sensitive_path(backup_dir, backup_mod.read_manifest(backup_dir), search)
    # Folder kerja (build paru, pacman.conf baru) hanya dibuat kalau rencana benar-benar dijalankan, dan dihapus di akhir.
    workdir = str(Path(tempfile.gettempdir()) / "distrorelay-<sementara>") if args.dry_run else provision.new_workdir()
    try:
        return _setup(args, kit, distro, runner, is_root, backup_dir, sensitive_file, workdir)
    finally:
        if not args.dry_run:
            shutil.rmtree(workdir, ignore_errors=True)


def _setup(args, kit, distro, runner, is_root, backup_dir, sensitive_file, workdir: str) -> int:
    plan = provision.build_plan(
        kit.profile,
        kit.packages,
        distro,
        is_root=is_root,
        has_paru=runner.which("paru"),
        home=Path.home(),
        stamp=datetime.now().strftime("%Y%m%d-%H%M%S"),
        workdir=workdir,
        backup_dir=backup_dir,
        sensitive_file=sensitive_file,
        decrypt=lambda path: backup_mod.decrypt(runner, path),
    )
    print(provision.plan_text(plan))
    note = _newer_kit_note(kit)
    if note:
        print(f"Perhatian: {note}\n")
    if distro.family is None:
        return 2
    if args.dry_run or not plan.steps:
        return 0
    if not args.yes and not _ask("Jalankan rencana di atas?"):
        print("Dibatalkan, tidak ada yang diubah.")
        return 1
    results = provision.execute(
        plan, runner, approve=(lambda question: True) if args.yes else _ask, progress=lambda m: print(f"\n== {m}")
    )
    print("\n" + provision.report_text(results))
    return 0 if all(r.status == "ok" for r in results) else 1


def cmd_backup(args) -> int:
    kit = _load_kit(args)
    if kit is None:
        return 1
    distro = distro_mod.read()
    runner = SubprocessRunner()
    print(f"Backup {distro.describe} ke {kit.backups_dir}")
    print("Ingat: ini hanya daftar paket + dotfiles, BUKAN backup dokumen/foto/kode (pakai rsync/restic/Déjà Dup).")
    try:
        result = backup_mod.create_backup(
            kit,
            runner,
            distro,
            home=Path.home(),
            curate=(lambda candidates: []) if args.yes else _curate,
            usb_backup_dirs=lambda: _usb_backup_dirs(runner),
            include_sensitive=not args.no_sensitive,
        )
    except (backup_mod.BackupError, OSError) as exc:
        print(f"Backup gagal: {exc}")
        return 1
    update = result.manifest["profile_update"]
    print(f"\nBackup tersimpan di {result.path}")
    print(f"  Nama logis: {len(update['native'])}; khusus {distro.family}: "
          f"{len(update['distro_specific'].get(distro.family, []))}; AUR: {len(update['aur'])}; "
          f"Flatpak: {len(update['flatpak'])}; tidak dibawa (kurasi): {len(update['exclude'])}")
    sensitive = result.manifest.get("sensitive")
    if sensitive:
        print(f"  File sensitif terenkripsi: {sensitive['count']} file, di "
              f"{result.path if sensitive['location'] == 'here' else sensitive['location']}")
    for note in result.notes:
        print(f"  - {note}")
    print("\nDi Windows: `distrorelay profile import <folder backup ini>` supaya profil ikut diperbarui.")
    return 0


def cmd_backups(args) -> int:
    kit = _load_kit(args)
    if kit is None:
        return 1
    backups = backup_mod.list_backups(kit.backups_dir)
    if not backups:
        print(f"Belum ada backup di {kit.backups_dir}")
        return 1
    for folder, manifest, problems in backups:
        status = "ok" if manifest and not problems else "RUSAK: " + "; ".join(problems)
        distro = (manifest or {}).get("distro", {}).get("pretty_name", "?")
        print(f"{folder.name}  {distro}  [{status}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"info": cmd_info, "kits": cmd_kits, "setup": cmd_setup, "backup": cmd_backup, "backups": cmd_backups}
    return handlers.get(args.command or "info")(args)
