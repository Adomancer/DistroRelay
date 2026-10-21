"""Ubah ScanReport jadi tampilan. Dipakai bersama oleh CLI (`scan`) dan dashboard TUI."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from rich.tree import Tree as RichTree

from .format import human_size
from .windows import bootrefs
from .windows.scanner import ScanReport

VERDICT_STYLES = {
    bootrefs.VERDICT_LEFTOVER: "bold yellow",
    bootrefs.VERDICT_IN_USE: "bold green",
    bootrefs.VERDICT_UNSURE: "bold magenta",
    bootrefs.VERDICT_PROTECTED: "bold cyan",
    bootrefs.VERDICT_NOT_TARGET: "dim",
}
KIND_TAGS = {
    "linux": "[Linux]",
    "windows": "[Windows]",
    "firmware": "[Firmware]",
    "unattached": "[?]",
    "other": "[ESP]",
}


@dataclass
class Node:
    label: Text
    children: list["Node"] = field(default_factory=list)
    expanded: bool = False


def _leaf(text: str, style: str = "") -> Node:
    return Node(Text(text, style=style))


def boot_order_node(report: ScanReport) -> Node:
    node = Node(Text("Urutan boot sekarang", style="bold"), expanded=True)
    analysis = report.analysis
    if analysis is None or not analysis.boot_order:
        node.children.append(_leaf("Belum terbaca (butuh Administrator)", "dim"))
        return node
    for entry in analysis.boot_order:
        label = Text(f"{entry.order + 1}. {entry.description}", style="bold" if entry.order == 0 else "")
        if entry.path:
            label.append(f"  {entry.path}", style="dim")
        label.append(f"  [{' + '.join(entry.sources)}]", style="dim")
        node.children.append(Node(label))
    first = analysis.boot_order[0]
    if not first.is_windows_boot_manager:
        node.children.append(
            _leaf(f"→ '{first.description}' ada di urutan pertama, jadi ini yang muncul saat laptop dinyalakan", "yellow")
        )
    return node


def installation_node(inst: bootrefs.Installation) -> Node:
    label = Text.assemble(
        (KIND_TAGS.get(inst.kind, "") + " ", "bold"),
        (inst.name, "bold"),
        " — ",
        (inst.verdict, VERDICT_STYLES.get(inst.verdict, "")),
    )
    node = Node(label, expanded=inst.kind == "linux")
    if inst.reasons:
        node.children.append(Node(Text("Alasan", style="italic"), [_leaf(f"• {r}") for r in inst.reasons], True))
    for comp in inst.components:
        text = Text(comp.label)
        if comp.size is not None and comp.kind != "partition":
            text.append(f"  ({human_size(comp.size)})", style="dim")
        if comp.protected:
            text.append("  [dikunci]", style="cyan")
        node.children.append(Node(text, [_leaf(e, "dim") for e in comp.evidence]))
    return node


def summary_nodes(report: ScanReport) -> list[Node]:
    nodes = [boot_order_node(report)]
    analysis = report.analysis
    if analysis is not None:
        nodes.extend(installation_node(inst) for inst in analysis.installations)
        if analysis.fallback is not None:
            fb = analysis.fallback
            nodes.append(
                Node(
                    Text.assemble(("File fallback firmware: ", "bold"), fb.path, " — ", (fb.owner, "bold")),
                    [
                        _leaf(f"Ukuran {human_size(fb.size)}", "dim"),
                        _leaf(
                            "Dipakai firmware kalau NVRAM ter-reset (mis. setelah update BIOS). "
                            "Kalau isinya bootloader distro yang sudah dihapus, laptop bisa gagal boot ke Windows.",
                            "dim",
                        ),
                    ],
                )
            )
        if analysis.notes:
            nodes.append(Node(Text("Catatan", style="bold"), [_leaf(f"• {n}") for n in analysis.notes], True))
    return nodes


def to_rich_tree(title: str, nodes: list[Node]) -> RichTree:
    tree = RichTree(Text(title, style="bold"))

    def add(parent: RichTree, node: Node) -> None:
        branch = parent.add(node.label)
        for child in node.children:
            add(branch, child)

    for node in nodes:
        add(tree, node)
    return tree


def partition_rows(report: ScanReport) -> list[tuple[Text, ...]]:
    rows = []
    if report.inventory is None:
        return rows
    for part in report.inventory.partitions:
        info = report.fsinfo.get(part.id)
        fs = info.fs_type if info else (part.filesystem or "")
        label = (info.label if info else part.label) or ""
        status = "dilindungi" if part.is_protected else ("Linux" if part.is_linux else part.category)
        rows.append(
            tuple(
                Text(str(v), style=style)
                for v, style in (
                    (part.id, "bold"),
                    (human_size(part.size), ""),
                    (f"{part.offset:,}", "dim"),
                    (part.type_name, ""),
                    (f"{part.drive_letter}:" if part.drive_letter else "", ""),
                    (fs, ""),
                    (label, ""),
                    ((info.last_mounted if info else "") or "", ""),
                    (status, "cyan" if part.is_protected else ("yellow" if part.is_linux else "")),
                )
            )
        )
    return rows


PARTITION_COLUMNS = ("ID", "Ukuran", "Offset", "Tipe GPT", "Drive", "FS", "Label", "Last-mounted", "Status")


def entry_rows(report: ScanReport) -> list[tuple[Text, ...]]:
    rows = []
    for entry in report.boot_entries:
        rows.append(
            (
                Text(str(entry.order + 1) if entry.order is not None else "-"),
                Text(entry.description, style="bold"),
                Text(entry.path or "(tanpa file)"),
                Text(entry.label, style="dim"),
                Text(" + ".join(entry.sources)),
                Text({True: "ya", False: "tidak", None: "?"}[entry.active]),
            )
        )
    return rows


ENTRY_COLUMNS = ("Urutan", "Nama", "Menjalankan", "ID", "Sumber", "Aktif")


def esp_nodes(report: ScanReport) -> list[Node]:
    esp = report.esp
    if esp is None:
        return [_leaf("ESP belum terbaca (butuh Administrator)", "dim")]
    root = Node(Text(f"ESP {esp.partition_id} · {human_size(esp.total_size)} terpakai", style="bold"), expanded=True)
    dirs: dict[str, Node] = {"": root}
    for directory in sorted(esp.dirs, key=lambda d: (d.count("\\"), d.lower())):
        parent_path, _, name = directory.rpartition("\\")
        parent = dirs.get(parent_path, root)
        node = Node(Text(f"{name}\\  ({human_size(esp.size_of(directory))})"), expanded=directory.count("\\") < 1)
        parent.children.append(node)
        dirs[directory] = node
    for f in esp.files:
        parent_path, _, name = f.path.rpartition("\\")
        dirs.get(parent_path, root).children.append(_leaf(f"{name}  ({human_size(f.size)})"))
    return [root]


CHECK_STYLES = {"ok": "green", "peringatan": "yellow", "gagal": "bold red", "perlu konfirmasi": "bold yellow"}


def preflight_text(result) -> Text:
    text = Text("Preflight\n", style="bold")
    for check in result.checks:
        text.append(f"  [{check.status}] ", style=CHECK_STYLES.get(check.status, ""))
        text.append(f"{check.title}: ", style="bold")
        text.append(f"{check.message}\n")
        for detail in check.details:
            text.append(f"      {detail}\n", style="dim")
    return text


def backup_text(result) -> Text:
    text = Text("Backup ", style="bold")
    text.append(f"{result.backup_id}\n", style="bold")
    text.append(f"  Lokal: {result.local_dir}\n")
    text.append(f"  USB:   {result.usb_dir or '-'}\n")
    for f in result.files:
        text.append(f"    {f.name:<24} {human_size(f.size):>9}  sha256 {f.sha256[:16]}…\n", style="dim")
    text.append(f"  Isi ESP: {len(result.esp_files)} file", style="dim")
    if result.skipped:
        text.append(f", {len(result.skipped)} dilewati karena dikunci Windows ({', '.join(result.skipped)})", style="dim")
    text.append("\n\nVerifikasi\n", style="bold")
    for check in result.verifications:
        text.append("  [ok]    " if check.ok else "  [GAGAL] ", style="green" if check.ok else "bold red")
        text.append(f"{check.name}: {check.message}\n")
    if not result.ok:
        text.append("\nBackup BELUM lengkap: lihat verifikasi yang gagal.\n", style="bold red")
    elif result.local_only:
        text.append("\nBackup lokal lengkap dan terverifikasi.\n", style="bold green")
        text.append(
            "Mode coba (--local-only): belum ada salinan di USB, jadi backup ini BELUM memenuhi syarat "
            "eksekusi M2b. Kalau SSD bermasalah atau Windows tidak bisa boot, salinan di C: ikut tidak terjangkau.\n",
            style="yellow",
        )
    else:
        text.append("\nBackup lengkap dan terverifikasi (lokal + USB).\n", style="bold green")
    return text


def candidates_text(analysis) -> Text:
    from .windows.uninstall_plan import SELECTABLE_KINDS

    text = Text("Kandidat yang bisa dipilih (tidak ada yang dipilih otomatis)\n", style="bold")
    for inst in analysis.installations:
        if inst.kind not in SELECTABLE_KINDS:
            continue
        text.append(f"\n  {inst.key}", style="bold cyan")
        text.append(f"  {inst.name} — ")
        text.append(f"{inst.verdict}\n", style=VERDICT_STYLES.get(inst.verdict, ""))
        for comp in inst.components:
            text.append(f"      {comp.ref:<28} {comp.label}\n", style="dim")
    text.append(
        "\nPilih dengan --select <key instalasi atau ref komponen>, boleh diulang. "
        "Contoh: distrohop plan --select efi:fedora --select efi:arch\n"
    )
    return text


def plan_text(plan) -> Text:
    text = Text("Rencana uninstall (DRY-RUN: tidak ada yang dijalankan)\n", style="bold")
    if not plan.actions:
        text.append("  Tidak ada aksi.\n", style="dim")
    for number, action in enumerate(plan.actions, 1):
        text.append(f"\n  {number}. {action.title}\n", style="bold")
        for command in action.commands:
            text.append(f"       $ {command}\n", style="cyan")
        for detail in action.details:
            text.append(f"       {detail}\n", style="dim")
    for label, items, style in (("Diblokir", plan.blocked, "red"), ("Peringatan", plan.warnings, "yellow")):
        if items:
            text.append(f"\n{label}\n", style=f"bold {style}")
            for item in items:
                text.append(f"  • {item}\n", style=style)
    return text


def verifications_text(title: str, checks) -> Text:
    text = Text(f"{title}\n", style="bold")
    for check in checks:
        text.append("  [ok]    " if check.ok else "  [GAGAL] ", style="green" if check.ok else "bold red")
        text.append(f"{check.name}: {check.message}\n")
    return text


def gate_text(gate) -> Text:
    text = Text("Gerbang sebelum eksekusi\n", style="bold")
    if gate.local_dir:
        text.append(f"  Backup lokal:     {gate.local_dir}\n", style="dim")
    if gate.external_dir:
        text.append(f"  Backup eksternal: {gate.external_dir}\n", style="dim")
    for note in gate.notes:
        text.append(f"  • {note}\n", style="dim")
    if gate.ok:
        text.append("  Semua syarat terpenuhi.\n", style="bold green")
    for problem in gate.problems:
        text.append(f"  [GAGAL] {problem}\n", style="bold red")
    return text


def execution_text(result) -> Text:
    text = Text(f"Eksekusi (sesi {result.session})\n", style="bold")
    for step in result.steps:
        text.append(f"  {step.step}. ", style="bold")
        text.append("[ok]    " if step.ok else "[GAGAL] ", style="green" if step.ok else "bold red")
        text.append(f"{step.title}: {step.message}\n")
    if result.completed:
        text.append("  Semua langkah selesai.\n", style="bold green")
    elif result.stopped:
        text.append(f"  {result.stopped}\n  Langkah sesudahnya TIDAK dijalankan.\n", style="bold red")
    text.append(f"  Journal: {result.journal_path}\n", style="dim")
    return text


def journal_text(sessions) -> Text:
    text = Text("Sesi eksekusi\n", style="bold")
    if not sessions:
        text.append("  Belum ada.\n", style="dim")
    for s in sessions:
        if not s.finished and s.resolved_by:
            status, style = f"terputus, dituntaskan oleh sesi {s.resolved_by}", "dim"
        elif not s.finished:
            status, style = "TERPUTUS", "bold yellow"
        elif s.ok:
            status, style = "selesai", "green"
        else:
            status, style = "berhenti karena gagal", "red"
        text.append(f"  {s.path.stem}  ", style="bold")
        text.append(f"{status}\n", style=style)
        if s.backup:
            text.append(f"      Backup: {s.backup}\n", style="dim")
        if s.post_check_ok is not None:
            text.append(f"      Post-check: {'ok' if s.post_check_ok else 'ADA YANG GAGAL'}\n",
                        style="dim" if s.post_check_ok else "red")
        if s.unfinished_step is not None:
            u = s.unfinished_step
            text.append(f"      Terputus saat langkah {u.step} ({u.kind} {u.ref or ''}) sedang berjalan\n", style="yellow")
        elif s.last_step is not None:
            text.append(f"      Langkah terakhir: {s.last_step.step} ({s.last_step.kind}) {s.last_step.status}\n", style="dim")
    return text


def info_text(report: ScanReport, log_path: str | None = None) -> Text:
    text = Text()
    text.append("Status\n", style="bold")
    text.append(f"  Hak Administrator: {'ya' if report.is_admin else 'TIDAK (scan terbatas)'}\n")
    text.append(f"  Waktu scan: {report.created_at} · DistroHop {report.version}\n")
    text.append(f"  Windows: {report.windows}\n")
    if report.inventory is not None:
        text.append(f"  Sidik jari layout disk: {report.inventory.fingerprint()[:16]}…\n", style="dim")
    if report.esp is not None and report.esp.temporary_mount:
        text.append(f"  ESP sempat di-mount sementara ke {report.esp.mounted_at} lalu di-unmount lagi\n", style="dim")

    text.append("\nBitLocker / Device Encryption\n", style="bold")
    if report.bitlocker is None:
        text.append("  Belum terbaca (butuh Administrator)\n", style="dim")
    elif not report.bitlocker:
        text.append("  Tidak ada volume yang dilaporkan\n")
    for volume in report.bitlocker or []:
        style = "bold yellow" if volume.needs_recovery_key else ""
        text.append(f"  {volume.drive_letter or '(tanpa huruf)'}: {volume.describe()}\n", style=style)

    text.append("\nRuang kosong (unallocated)\n", style="bold")
    regions = report.inventory.free_regions() if report.inventory else []
    if not regions:
        text.append("  Tidak ada ruang kosong yang berarti\n")
    for region in regions:
        text.append(f"  disk{region.disk_number}: {human_size(region.size)} mulai offset {region.offset:,}\n")

    text.append("\nLangkah scan yang dilewati / gagal\n", style="bold")
    if not report.issues:
        text.append("  Tidak ada\n")
    for issue in report.issues:
        style = {"error": "red", "warning": "yellow"}.get(issue.severity, "dim")
        text.append(f"  [{issue.step}] {issue.message}\n", style=style)
    if log_path:
        text.append(f"\nLog: {log_path}\n", style="dim")
    return text
