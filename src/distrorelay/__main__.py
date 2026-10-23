"""Entry point: `python -m distrorelay [perintah]`. Tiap perintah ada di paket `cli`; di sini hanya logging, elevasi
admin, dan pemilihan handler."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path
from typing import Callable

from .cli import backup, iso, linux, prompts, scan, system, uninstall
from .cli.parser import NO_ADMIN_COMMANDS, PAUSE_COMMANDS, build_parser
from .logging_setup import setup_logging

log = logging.getLogger("distrorelay")


def _handlers(log_path: Path | None) -> dict[str | None, Callable[[argparse.Namespace], int]]:
    def tui(args: argparse.Namespace) -> int:
        from .tui.app import run_tui

        return run_tui(str(log_path) if log_path else None)

    return {
        "scan": lambda args: scan.cmd_scan(args, log_path),
        "plan": scan.cmd_plan,
        "backup": backup.cmd_backup,
        "verify-backup": backup.cmd_verify_backup,
        "uninstall": uninstall.cmd_uninstall,
        "restore": uninstall.cmd_restore,
        "journal": uninstall.cmd_journal,
        "profile": linux.cmd_profile,
        "kit": linux.cmd_kit,
        "fast-startup": linux.cmd_fast_startup,
        "iso": iso.cmd_iso,
        "shrink-c": system.cmd_shrink,
        "selftest": lambda args: system.cmd_selftest(),
        "tui": tui,
        None: tui,  # tanpa perintah = dashboard
    }


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

    if not is_admin() and not args.no_elevate and args.command not in NO_ADMIN_COMMANDS:
        print("DistroRelay butuh hak Administrator untuk membaca ESP, NVRAM, dan header partisi.")
        print("Membuka jendela Administrator (konfirmasi di dialog UAC); jendela ini menunggu sampai selesai…")
        try:
            code = relaunch_as_admin(["--elevated-child", *argv])
        except KeyboardInterrupt:
            print("Berhenti menunggu. Jendela Administrator tetap berjalan.")
            return 130
        if code is None:
            print("Permintaan Administrator dibatalkan.")
            print("Untuk scan terbatas tanpa admin: python -m distrorelay --no-elevate scan")
            return 1
        return code

    try:
        code = _handlers(log_path)[args.command](args)
    except KeyboardInterrupt:
        code = 130
    except Exception:
        log.exception("Error tidak terduga")
        traceback.print_exc()
        code = 1
        if args.elevated_child:
            prompts.pause()
        return code
    if args.elevated_child and args.command in PAUSE_COMMANDS:
        prompts.pause()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
