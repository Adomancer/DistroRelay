"""`iso`: ISO manager (M6)."""

from __future__ import annotations

import argparse


def _pick_drive(letter: str | None, volumes, *, required: bool):
    """(volume, catatan). Tanpa --drive: satu-satunya USB berlabel Ventoy. Volume None = tidak ada / ditolak."""
    from ..windows.iso import catalog

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


def cmd_iso(args: argparse.Namespace, tools=None, volumes=None) -> int:
    from rich.console import Console

    from .. import render
    from ..format import human_size
    from ..windows.iso import IsoError, catalog, manager, saved_releases, sources, verify
    from ..windows.iso.download import DownloadStopped, fetch

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
        with console.status("Mengecek versi terbaru…") as status:
            rows = catalog.check(sources.SOURCES, tools.fetch if tools else fetch,
                                 drive=drive.root if drive else None, downloads_root=tools.root if tools else None,
                                 progress=lambda left: status.update(f"Mengecek versi terbaru… menunggu {', '.join(left)}"))
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
                    say=lambda message: console.print(message, style="dim", markup=False),
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
        note = None if result.ok else manager.rejected_note(saved.iso_path)
        if note:
            console.print(f"  {note}", style="dim", markup=False)
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
