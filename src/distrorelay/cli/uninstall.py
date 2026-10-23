"""`uninstall`, `restore`, dan `journal`: lewat UninstallFlow / RestoreFlow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import prompts
from .scan import scan_with_status


def cmd_journal(args: argparse.Namespace, journal_root: Path | None = None) -> int:
    from rich.console import Console

    from .. import render
    from ..windows.journal import journal_dir, sessions
    from ..windows.workflow import RESOLVE_WORD, mark_session_resolved

    console = Console()
    directory = journal_root or journal_dir()
    name = getattr(args, "tuntaskan", None)
    if not name:
        console.print(render.journal_text(sessions(directory)))
        return 0
    found = [s for s in sessions(directory) if s.path.stem == name]
    if not found:
        console.print(f"Sesi {name} tidak ada di {directory}.", style="red", markup=False)
        return 1
    console.print(render.journal_text(found))
    if not found[0].needs_attention:
        console.print("Sesi ini tidak sedang butuh perhatian; tidak ada yang diubah.")
        return 0
    console.print(
        "Tandai tuntas hanya kalau kondisinya sudah kamu cek: scan ulang (`distrorelay scan`), Windows boot normal, dan "
        "yang setengah jadi sudah dipulihkan atau memang dibiarkan. Sesi ini lalu tidak lagi meminta LANJUT.",
        style="bold yellow", markup=False,
    )
    problem = mark_session_resolved(directory, name, prompts.ask_word(f"Ketik {RESOLVE_WORD}: "), "ditandai lewat CLI")
    if problem:
        console.print(problem, style="red", markup=False)
        return 1
    console.print(f"Sesi {name} ditandai tuntas.", style="green", markup=False)
    return 0


def cmd_uninstall(args: argparse.Namespace, flow=None) -> int:
    from rich.console import Console

    from .. import render
    from ..windows.stopping import StopSignal, console_stop_guard
    from ..windows.workflow import CONTINUE_WORD, DELETE_WORD, FlowError, UninstallFlow, default_deps, typed

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
        if not flow.acknowledge_unfinished(prompts.ask_word(f"Tetap lanjut? Ketik {CONTINUE_WORD}: ")):
            return 1
    if not args.backup:
        available = [b.describe() for b in flow.existing_backups()]
        console.print("Wajib --backup <ID>. Backup yang ada: " + ("\n  " + "\n  ".join(available) if available else "-"),
                      style="red", markup=False)
        return 1
    if not args.select:
        console.print("Wajib --select (lihat kandidat dengan `distrorelay plan`).", style="red")
        return 1

    # Syarat proses dari plan yang tidak bisa dicek kode: test VM dan latihan prosedur darurat.
    choice = prompts.ask("Ini dijalankan di VM (untuk test) atau di laptop asli? Ketik vm atau asli: ").lower()
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
        answers[key] = prompts.ask(f"{question} Ketik ya: ").lower() == "ya"
        if not answers[key]:
            break
    if flow.set_mode(choice, answers):
        console.print("Selesaikan dulu di VM. Tidak ada yang diubah.", style="yellow")
        return 1

    report = scan_with_status(console, flow)
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
        flow.confirm(check.key, prompts.ask("Sudah? Ketik ya: ").lower() == "ya")

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
        typed_words[required.key] = prompts.ask_word(f"{required.title} (ketik {required.word}): ")
        if not typed(typed_words[required.key], required.word):
            console.print("Tidak cocok. Dibatalkan, tidak ada yang diubah.")
            return 1
    ticket = flow.authorize(prompts.ask_word(f"Ketik {DELETE_WORD} untuk menjalankan, atau Enter untuk batal: "), typed_words)
    if ticket is None:
        console.print("Dibatalkan. Tidak ada yang diubah.")
        return 1

    stop = StopSignal()
    with console.status("Menjalankan… (Ctrl+C = berhenti setelah langkah yang sedang jalan)") as status, \
            console_stop_guard(stop, notify=lambda m: status.update(m)):
        try:
            result = flow.execute(ticket, progress=lambda m: status.update(m), stop=stop)
        except FlowError as exc:
            result = None
            failure = str(exc)
    if result is None:
        console.print(f"Tidak dijalankan: {failure}", style="bold red", markup=False)
        return 1
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


def cmd_restore(args: argparse.Namespace, flow=None) -> int:
    from rich.console import Console

    from .. import render
    from ..windows.workflow import RESTORE_WORD, RestoreFlow, default_deps

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
    ticket = flow.authorize(prompts.ask_word(f"Ketik {RESTORE_WORD} untuk menjalankan: "))
    if ticket is None:
        console.print("Dibatalkan.")
        return 1
    from ..windows.stopping import StopSignal, console_stop_guard

    stop = StopSignal()
    with console.status("Memulihkan… (Ctrl+C = berhenti setelah langkah yang sedang jalan)") as status, \
            console_stop_guard(stop, notify=lambda m: status.update(m)):
        results = flow.run(ticket, progress=lambda m: status.update(m), stop=stop)
    console.print(render.verifications_text("Pemulihan", results))
    return 0 if all(r.ok for r in results) else 1
