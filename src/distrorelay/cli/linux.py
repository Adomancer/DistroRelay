"""`profile`, `kit`, dan `fast-startup` (Linux kit, M4-M5)."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import prompts


def cmd_profile(args: argparse.Namespace) -> int:
    from rich.console import Console

    from .. import render
    from ..common import profile as profile_mod
    from ..windows import kit_export

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

    from ..common import profile as profile_mod

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
    if not yes and prompts.ask("Tambahkan ke profil? Ketik ya: ").lower() != "ya":
        console.print("Dibatalkan, profil tidak berubah.")
        return 1
    backup_path = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    backup_path.write_bytes(path.read_bytes())
    path.write_text(profile_mod.to_toml(merged), encoding="utf-8")
    console.print(f"Profil diperbarui ({path}); versi lama (termasuk komentarnya) di {backup_path}. "
                  "Export ulang kit supaya ikut ke Linux.", markup=False)
    return 0


def cmd_kit(args: argparse.Namespace) -> int:
    from rich.console import Console

    from .. import render
    from ..common import profile as profile_mod
    from ..windows import kit_export

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


def cmd_fast_startup(args: argparse.Namespace) -> int:
    from ..windows import kit_export

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
