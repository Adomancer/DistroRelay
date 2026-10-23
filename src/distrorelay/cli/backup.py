"""`backup` dan `verify-backup`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .scan import scan_with_status


def cmd_backup(args: argparse.Namespace) -> int:
    from rich.console import Console

    from .. import render
    from ..windows import disks, esp
    from ..windows.backup import BackupError, backup_runner, create_backup
    from ..windows.preflight import run_preflight
    from ..windows.runner import SubprocessRunner

    console = Console()
    report = scan_with_status(console)
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


def cmd_verify_backup(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.text import Text

    from ..windows.backup import verify_backup_dir

    console = Console()
    checks = verify_backup_dir(Path(args.directory))
    for check in checks:
        line = Text("[ok]    " if check.ok else "[GAGAL] ", style="green" if check.ok else "bold red")
        line.append(f"{check.name}: {check.message}")
        console.print(line)
    return 0 if all(c.ok for c in checks) else 1
