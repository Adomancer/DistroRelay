"""`selftest` dan `shrink-c`."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import prompts


def cmd_selftest() -> int:
    from rich.console import Console
    from rich.text import Text

    from .. import selftest

    console = Console()
    with console.status("Selftest…"):
        checks = selftest.run()
    for check in checks:
        line = Text("[ok]    " if check.ok else "[GAGAL] ", style="green" if check.ok else "bold red")
        line.append(f"{check.name}: {check.message}")
        console.print(line)
    return 0 if all(c.ok for c in checks) else 1


def cmd_shrink(args: argparse.Namespace, inner=None, journal_root: Path | None = None) -> int:
    from rich.console import Console

    from .. import render
    from ..format import human_size
    from ..windows import shrink
    from ..windows.journal import incomplete_sessions
    from ..windows.runner import ReadOnlyRunner, SubprocessRunner
    from ..windows.workflow import CONTINUE_WORD, typed

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
        if not typed(prompts.ask_word(f"Ada sesi yang belum tuntas. Tetap lanjut? Ketik {CONTINUE_WORD}: "), CONTINUE_WORD):
            return 1
    console.print(
        "\nIni MENGUBAH tabel partisi (sama seperti Shrink Volume di Disk Management). Colokkan charger dan jangan "
        "matikan laptop selama proses. Sebaiknya dicoba di VM dulu.",
        style="bold yellow",
    )
    if not typed(prompts.ask_word(f"Ketik {shrink.SHRINK_WORD} untuk menjalankan, atau Enter untuk batal: "), shrink.SHRINK_WORD):
        console.print("Dibatalkan. Tidak ada yang diubah.")
        return 1
    from ..windows.stopping import StopSignal, console_stop_guard

    stop = StopSignal()
    # Ctrl+C / tutup jendela tidak boleh memotong Resize-Partition di tengah: penangan ini menahannya sampai selesai.
    with console.status("Mengecilkan C: … (jangan matikan laptop; Ctrl+C ditahan sampai selesai)") as status, \
            console_stop_guard(stop, notify=lambda m: status.update("Ditunggu sampai Resize-Partition selesai…")):
        result = shrink.execute(plan, inner, journal_root=journal_root, stop=stop)
    console.print(result.message, style="green" if result.ok else "bold red", markup=False)
    return 0 if result.ok else 1
