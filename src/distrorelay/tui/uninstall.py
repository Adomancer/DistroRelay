"""Wizard uninstall (M2c fase B). Setiap layar hanya memanggil `UninstallFlow`; urutan & syaratnya dipaksa di sana.

Sesi belum tuntas (LANJUT) → 1 mode → 2 pilih → 3 preflight → 4 backup → 5 dry-run + gerbang + HAPUS → 6 eksekusi.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, OptionList, RadioButton, RadioSet, RichLog, Static, Tree
from textual.widgets.option_list import Option

from .. import render
from ..format import human_size
from ..windows.stopping import StopSignal, console_stop_guard
from ..windows.workflow import (
    CONTINUE_WORD,
    DELETE_WORD,
    REAL_MACHINE_STATEMENTS,
    Candidate,
    FlowError,
    UninstallFlow,
)
from .common import StepScreen, log, safe_call_from_thread

STEPS = 6


def first_screen(flow: UninstallFlow) -> StepScreen:
    unfinished = flow.unfinished_sessions()
    return UnfinishedScreen(flow, unfinished) if unfinished else ModeScreen(flow)


# --- 0. Sesi belum tuntas ---------------------------------------------------


class UnfinishedScreen(StepScreen):
    STEP = "Sebelum mulai · Ada sesi uninstall yang belum tuntas"
    HELP = (
        "Sesi di bawah terputus, dihentikan, gagal, atau post-check-nya gagal, jadi sistem mungkin setengah jadi. "
        "Wizard akan scan ulang dulu, jadi kondisi sekarang yang dipakai. Untuk lanjut, ketik "
        f"{CONTINUE_WORD} (huruf besar)."
    )

    def __init__(self, flow: UninstallFlow, sessions) -> None:
        super().__init__(flow)
        self.sessions = sessions

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="body"):
            yield Static(render.journal_text(self.sessions))
        yield Input(placeholder=f"Ketik {CONTINUE_WORD}", id="word")

    def on_mount(self) -> None:
        self.query_one("#word", Input).focus()

    @on(Input.Changed, "#word")
    def _typed(self) -> None:
        self.refresh_next()

    @on(Input.Submitted, "#word")
    def _submitted(self) -> None:
        self.next()

    def refresh_next(self) -> None:
        self.set_next(bool(self.query_one("#word", Input).value))

    def next(self) -> None:
        if self.flow.acknowledge_unfinished(self.query_one("#word", Input).value):
            self.app.push_screen(ModeScreen(self.flow))
        else:
            self.set_status(f"Ketik {CONTINUE_WORD} persis: huruf besar, tanpa spasi di depan.", "bold red")


# --- 1. Mode ------------------------------------------------------------------


class ModeScreen(StepScreen):
    STEP = f"Langkah 1/{STEPS} · VM atau laptop asli?"
    HELP = (
        "Mode VM hanya berlaku kalau komputer ini memang terdeteksi sebagai mesin virtual. Di mesin fisik, mode "
        "otomatis jadi 'laptop asli' dan butuh dua pernyataan. Mode tidak pernah melonggarkan syarat backup & gerbang."
    )

    def __init__(self, flow: UninstallFlow) -> None:
        super().__init__(flow)
        self.choice: str | None = None
        self.decision = None

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="body"):
            with RadioSet(id="mode"):
                yield RadioButton("VM (untuk test)", id="mode-vm")
                yield RadioButton("Laptop asli", id="mode-asli")
            yield Static("", id="machine")
            yield Vertical(id="statements")

    def on_mount(self) -> None:
        self.query_one("#mode", RadioSet).focus()

    @on(RadioSet.Changed, "#mode")
    def _mode_changed(self, event: RadioSet.Changed) -> None:
        choice = "vm" if event.pressed.id == "mode-vm" else "asli"
        self.choice, self.decision = choice, None
        self.refresh_next()
        self.background(lambda: self.flow.decide_mode(choice), lambda d: self._show(choice, d), "Mengenali mesin (WMI)…")

    async def _show(self, choice: str, decision) -> None:
        if choice != self.choice:  # pilihan sudah diganti selagi deteksi berjalan
            return
        self.decision = decision
        machine = decision.machine
        text = Text(f"Terdeteksi: {machine.description} ({'mesin virtual' if machine.is_vm else 'mesin fisik'})\n")
        if decision.forced_real:
            text.append("Ini bukan VM, jadi diperlakukan sebagai laptop asli.\n", style="bold yellow")
        self.query_one("#machine", Static).update(text)
        box = self.query_one("#statements", Vertical)
        await box.remove_children()
        if decision.statements:
            await box.mount(
                Static(Text("Pernyataan wajib (centang = ya)", style="bold")),
                *(Checkbox(question, id=f"st-{key}") for key, question in decision.statements.items()),
            )
        else:
            await box.mount(Static(Text("Mode VM: tidak ada pernyataan tambahan.", style="dim")))
        self.set_status("")
        self.refresh_next()

    def refresh_next(self) -> None:
        self.set_next(self.decision is not None)

    def next(self) -> None:
        answers = {key: self.query_one(f"#st-{key}", Checkbox).value for key in self.decision.statements}
        missing = self.flow.set_mode(self.choice, answers)
        if missing:
            self.set_status(
                "Selesaikan dulu di VM: " + " ".join(REAL_MACHINE_STATEMENTS[k] for k in missing) + " Tidak ada yang diubah.",
                "bold yellow",
            )
            return
        self.app.push_screen(SelectScreen(self.flow))


# --- 2. Pilih -------------------------------------------------------------------


class CheckTree(Tree):
    """Tree kandidat dengan ceklis. Spasi = centang/lepas; di baris instalasi: semua komponennya yang bisa dipilih."""

    BINDINGS = [Binding("space", "toggle_check", "Centang/lepas")]

    class Toggled(Message):
        pass

    def load(self, candidates: list[Candidate]) -> None:
        self.clear()
        self.root.expand()
        self._components = []
        groups: dict[str, tuple] = {}
        for candidate in candidates:
            groups.setdefault(candidate.installation.key, (candidate.installation, []))[1].append(candidate)
        for inst, members in groups.values():
            inst_node = self.root.add("", data={"kind": "installation", "inst": inst, "members": []}, expand=True)
            if inst.reasons:
                reasons = inst_node.add(Text("Alasan", style="italic"))
                for reason in inst.reasons:
                    reasons.add_leaf(Text(f"• {reason}"))
            for candidate in members:
                data = {"kind": "component", "candidate": candidate, "checked": False}
                details = ([Text(f"Tidak bisa dipilih: {candidate.blocked}", style="italic yellow")] if candidate.blocked else [])
                details += [Text(evidence, style="dim") for evidence in candidate.component.evidence]
                node = inst_node.add("", data=data) if details else inst_node.add_leaf("", data=data)
                for detail in details:
                    node.add_leaf(detail)
                inst_node.data["members"].append(node)
                self._components.append(node)
            self._relabel(inst_node)

    def _relabel(self, inst_node) -> None:
        members = inst_node.data["members"]
        selectable = [n for n in members if not n.data["candidate"].blocked]
        checked = [n for n in selectable if n.data["checked"]]
        mark = "[x]" if selectable and len(checked) == len(selectable) else ("[-]" if checked else "[ ]")
        inst = inst_node.data["inst"]
        inst_node.set_label(
            Text.assemble(
                (f"{mark} ", "bold"),
                (render.KIND_TAGS.get(inst.kind, "") + " ", "bold"),
                (inst.name, "bold"),
                " — ",
                (inst.verdict, render.VERDICT_STYLES.get(inst.verdict, "")),
                (f"  ({inst.key})", "dim"),
            )
        )
        for node in members:
            candidate = node.data["candidate"]
            comp = candidate.component
            if candidate.blocked:
                label = Text(f"[·] {comp.label}", style="dim")
            else:
                label = Text(f"{'[x]' if node.data['checked'] else '[ ]'} {comp.label}")
                if comp.size is not None and comp.kind != "partition":
                    label.append(f"  ({human_size(comp.size)})", style="dim")
            node.set_label(label)

    def action_toggle_check(self) -> None:
        node = self.cursor_node
        data = node.data if node is not None else None
        if not data:
            return
        if data["kind"] == "component":
            candidate = data["candidate"]
            if candidate.blocked:
                self.notify(candidate.blocked, title="Tidak bisa dipilih", severity="warning", markup=False)
                return
            data["checked"] = not data["checked"]
            inst_node = node.parent
        else:
            selectable = [n for n in data["members"] if not n.data["candidate"].blocked]
            target = not all(n.data["checked"] for n in selectable)
            for member in selectable:
                member.data["checked"] = target
            inst_node = node
        self._relabel(inst_node)
        self.post_message(self.Toggled())

    def checked_refs(self) -> list[str]:
        return [n.data["candidate"].component.ref for n in getattr(self, "_components", []) if n.data["checked"]]


class SelectScreen(StepScreen):
    STEP = f"Langkah 2/{STEPS} · Pilih yang mau dihapus"
    HELP = (
        "Tidak ada yang dicentang otomatis. Spasi = centang/lepas (di baris instalasi: semua komponennya), "
        "Enter = buka/tutup detail. Rencana di kanan ikut berubah; belum ada yang dijalankan."
    )

    def compose_body(self) -> ComposeResult:
        with Horizontal(classes="body"):
            yield CheckTree(Text("Kandidat"), id="candidates")
            with VerticalScroll(id="plan-pane"):
                yield Static(id="plan")

    def on_mount(self) -> None:
        self.background(lambda: self.flow.scan(progress=self.progress), self._scanned, "Scan…")

    def _scanned(self, report) -> None:
        if report.analysis is None:
            self.set_status("Analisis tidak tersedia (scan butuh hak Administrator).", "bold red")
            return
        tree = self.query_one("#candidates", CheckTree)
        tree.load(self.flow.candidates())
        tree.focus()
        status = Text(f"Scan selesai {report.created_at}. ")
        for issue in report.issues:
            if issue.severity != "info":
                status.append(f"\n[{issue.step}] {issue.message}", style="yellow")
        self.set_status(status)
        self._update_plan()

    @on(CheckTree.Toggled)
    def _toggled(self) -> None:
        self._update_plan()

    def _update_plan(self) -> None:
        refs = self.query_one("#candidates", CheckTree).checked_refs()
        plan = self.flow.select(refs)
        self.query_one("#plan", Static).update(
            render.plan_text(plan) if refs else Text("Belum ada yang dipilih.", style="dim")
        )
        self.refresh_next()

    def refresh_next(self) -> None:
        plan = self.flow.plan
        self.set_next(plan is not None and bool(plan.actions) and not self.flow.unknown)

    def next(self) -> None:
        self.app.push_screen(PreflightScreen(self.flow))


# --- 3. Preflight -------------------------------------------------------------


class PreflightScreen(StepScreen):
    STEP = f"Langkah 3/{STEPS} · Preflight"
    HELP = (
        "Cek otomatis sebelum backup. Yang berstatus 'perlu konfirmasi' wajib kamu centang sendiri, "
        "misalnya USB darurat yang sudah dicoba boot sampai Command Prompt."
    )

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="body"):
            yield Static(id="checks")
            yield Vertical(id="confirmations")
            yield Button("Cek ulang", id="recheck")

    def on_mount(self) -> None:
        self._run()

    def _run(self) -> None:
        self.background(self.flow.run_preflight, self._show, "Preflight…")

    @on(Button.Pressed, "#recheck")
    def _recheck(self) -> None:
        if not self.busy:
            self._run()

    async def _show(self, result) -> None:
        self.query_one("#checks", Static).update(render.preflight_text(result))
        box = self.query_one("#confirmations", Vertical)
        await box.remove_children()
        if result.needs_confirmation:
            await box.mount(
                Static(Text("Konfirmasi manual (baca pesannya di atas, centang = sudah)", style="bold")),
                *(Checkbox(f"Sudah: {c.title}", id=f"confirm-{c.key}") for c in result.needs_confirmation),
            )
        if result.blocking:
            self.set_status(
                f"Tidak bisa lanjut: {', '.join(c.title for c in result.blocking)} gagal. Perbaiki, lalu tekan Cek ulang.",
                "bold red",
            )
        else:
            self.set_status("Preflight selesai.")
        self.refresh_next()

    @on(Checkbox.Changed)
    def _confirmed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("confirm-"):
            self.flow.confirm(event.checkbox.id.removeprefix("confirm-"), event.value)
            self.refresh_next()

    def refresh_next(self) -> None:
        result = self.flow.preflight
        self.set_next(
            result is not None
            and not result.blocking
            and all(c.key in self.flow.confirmed for c in result.needs_confirmation)
        )

    def next(self) -> None:
        self.app.push_screen(BackupScreen(self.flow))


# --- 4. Backup --------------------------------------------------------------------


class BackupScreen(StepScreen):
    STEP = f"Langkah 4/{STEPS} · Backup"
    HELP = (
        "Wajib sebelum menghapus apa pun: ESP, BCD, entri firmware, NVRAM, dan metadata partisi disimpan di C: dan "
        "disalin ke drive eksternal, lalu diverifikasi. Bisa juga memakai backup lama yang punya salinan eksternal; "
        "langkah berikutnya memverifikasinya ulang."
    )

    def __init__(self, flow: UninstallFlow) -> None:
        super().__init__(flow)
        self.targets: list[Path] = flow.backup_targets()

    def _target_label(self, target: Path) -> str:
        letter = str(target)[:1].upper()
        info = next((t for t in (self.flow.preflight.usb_targets if self.flow.preflight else []) if t.drive == letter), None)
        if info is None:
            return str(target)
        return f"{info.drive}:  {info.label or '(tanpa label)'} · {info.filesystem or '?'} · kosong {human_size(info.free)} · {info.disk}"

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="body"):
            yield Static(Text("Buat backup baru ke drive eksternal", style="bold"))
            if self.targets:
                yield RadioSet(*(RadioButton(self._target_label(t)) for t in self.targets), id="targets")
                yield Button("Buat backup baru", id="make", variant="primary")
            else:
                yield Static(Text("Tidak ada drive eksternal (disk fisik selain SSD Windows).", style="yellow"))
            yield Static(Text("\nAtau pakai backup yang sudah ada", style="bold"))
            yield OptionList(id="existing")
            yield Static(id="result")

    def on_mount(self) -> None:
        self._fill_existing()

    def _fill_existing(self) -> None:
        existing = self.query_one("#existing", OptionList)
        existing.clear_options()
        backups = self.flow.existing_backups()
        for backup in backups:
            existing.add_option(Option(backup.describe(), id=backup.backup_id, disabled=not backup.usb_copy))
        if not backups:
            existing.add_option(Option("Belum ada backup", disabled=True))

    @on(Button.Pressed, "#make")
    def _make(self) -> None:
        if self.busy:
            return
        index = self.query_one("#targets", RadioSet).pressed_index
        if index < 0:
            self.set_status("Pilih drive tujuannya dulu.", "bold yellow")
            return
        target = self.targets[index]
        self.background(lambda: self.flow.make_backup(target, progress=self.progress), self._made, "Backup…")

    def _made(self, result) -> None:
        self.query_one("#result", Static).update(render.backup_text(result))
        if result.ok:
            self.set_status(f"Backup {result.backup_id} lengkap dan terverifikasi.", "bold green")
        else:
            self.set_status("Backup belum lengkap: lihat verifikasi yang gagal di bawah.", "bold red")
        self._fill_existing()
        self.refresh_next()

    @on(OptionList.OptionSelected, "#existing")
    def _use(self, event: OptionList.OptionSelected) -> None:
        if self.busy or event.option.id is None:
            return
        self.flow.use_backup(event.option.id)
        self.query_one("#result", Static).update("")
        self.set_status(f"Memakai backup {event.option.id}; diverifikasi ulang di langkah berikutnya.", "bold green")
        self.refresh_next()

    def refresh_next(self) -> None:
        self.set_next(self.flow.backup_id is not None)

    def next(self) -> None:
        self.app.push_screen(ConfirmScreen(self.flow))


# --- 5. Dry-run, gerbang, HAPUS -------------------------------------------------


class ConfirmScreen(StepScreen):
    STEP = f"Langkah 5/{STEPS} · Dry-run, gerbang, dan konfirmasi"
    HELP = (
        "Baca rencana baris per baris. Gerbang memverifikasi ulang backup lokal + eksternal dan memastikan "
        "ESP, entri boot, dan layout disk tidak berubah sejak scan."
    )
    NEXT_LABEL = "Jalankan"

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="body"):
            yield Static(render.plan_text(self.flow.plan), id="plan")
            yield Static(id="gate")
        yield Input(placeholder=f"Ketik {DELETE_WORD} untuk menjalankan", id="word", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#next", Button).variant = "error"
        self.background(self.flow.gate, self._gated, "Memeriksa gerbang (verifikasi backup lokal & eksternal)…")

    def _gated(self, gate) -> None:
        self.query_one("#gate", Static).update(render.gate_text(gate))
        word = self.query_one("#word", Input)
        if gate.ok:
            word.disabled = False
            word.focus()
            self.set_status("Ini akan MENGHAPUS entri boot dan folder ESP sesuai rencana di atas.", "bold red")
        else:
            self.set_status("Gerbang belum lolos: kembali ke langkah sebelumnya dan perbaiki.", "bold red")
        self.refresh_next()

    @on(Input.Changed, "#word")
    def _typed(self) -> None:
        self.refresh_next()

    @on(Input.Submitted, "#word")
    def _submitted(self) -> None:
        if not self.busy:
            self.next()

    def refresh_next(self) -> None:
        gate = self.flow.gate_result
        self.set_next(gate is not None and gate.ok and bool(self.query_one("#word", Input).value))

    def next(self) -> None:
        gate = self.flow.gate_result
        if gate is None or not gate.ok:
            return
        ticket = self.flow.authorize(self.query_one("#word", Input).value)
        if ticket is None:
            self.set_status(f"Ketik {DELETE_WORD} persis (huruf besar). Tidak ada yang diubah.", "bold red")
            return
        self.app.push_screen(ExecuteScreen(self.flow, ticket))


# --- 6. Eksekusi & post-check ---------------------------------------------------


class ExecuteScreen(StepScreen):
    STEP = f"Langkah 6/{STEPS} · Eksekusi & post-check"
    HELP = (
        "Ctrl+C, Esc, atau Ctrl+Q = berhenti dengan rapi setelah langkah yang sedang jalan selesai. "
        "Menutup jendela juga ditangani, tapi Windows hanya memberi waktu ±5 detik."
    )
    NEXT_LABEL = "Kembali ke dashboard"
    BINDINGS = [
        Binding("ctrl+c", "request_stop('Ctrl+C')", "Berhenti setelah langkah ini", priority=True),
        Binding("escape", "request_stop('Esc')", "Berhenti", show=False),
    ]

    def __init__(self, flow: UninstallFlow, ticket) -> None:
        super().__init__(flow)
        self.ticket = ticket
        self.stop = StopSignal()
        self.executing = False
        self.finished = False

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(classes="body"):
            yield RichLog(id="log", wrap=True, markup=False, auto_scroll=True)
            yield Static(id="result")

    def on_mount(self) -> None:
        for button_id in ("#back", "#cancel"):
            self.query_one(button_id, Button).display = False
        self.app.stop_signal = self.stop
        self.executing = True
        self.set_busy(True)
        self.set_status("Menjalankan… (Ctrl+C = berhenti setelah langkah yang sedang jalan)", "bold")
        self.run_worker(self._run, thread=True, exclusive=True, group="execute", exit_on_error=False)

    def _write(self, message: str, style: str = "") -> None:
        self.query_one("#log", RichLog).write(Text(message, style=style))

    def _run(self) -> None:
        app = self.app
        say = lambda message: safe_call_from_thread(app, self._write, message)  # noqa: E731
        try:
            try:
                with console_stop_guard(self.stop, notify=lambda m: safe_call_from_thread(app, self._write, m, "yellow")):
                    result = self.flow.execute(self.ticket, progress=say, stop=self.stop)
            finally:
                self.executing = False
            safe_call_from_thread(app, self._executed, result)
            if result.stopped_by_user:
                return
            app.quit_blocker = "Post-check sedang berjalan (scan ulang); tunggu sampai selesai."
            say("Post-check (scan ulang)…")
            checks = self.flow.post_check(progress=self.progress)
            safe_call_from_thread(app, self._checked, result, checks)
        except FlowError as exc:  # tiket tidak berlaku: ditolak sebelum journal dibuka, tidak ada yang diubah
            safe_call_from_thread(app, self.set_status, f"Tidak dijalankan: {exc}. Tidak ada yang diubah.", "bold red")
        except Exception as exc:  # termasuk simulasi crash (build dev): journal menunjukkan sesi TERPUTUS
            log.exception("Eksekusi dari TUI berhenti karena error")
            safe_call_from_thread(app, self._crashed, exc)
        finally:
            safe_call_from_thread(app, self._finish)

    def _executed(self, result) -> None:
        self.app.stop_signal = None
        self.query_one("#result", Static).update(render.execution_text(result))
        if result.stopped_by_user:
            self.set_status(
                "Dihentikan dengan rapi: langkah yang sudah jalan tetap berlaku, sisanya belum dijalankan, dan Windows "
                "tetap bisa boot. Untuk melanjutkan, jalankan wizard lagi (dengan backup baru); untuk membatalkan yang "
                "sudah dihapus, pakai Pulihkan (p) di dashboard.",
                "bold yellow",
            )

    def _checked(self, result, checks) -> None:
        text = render.execution_text(result)
        text.append("\n")
        text.append(render.verifications_text("Post-check", checks))
        self.query_one("#result", Static).update(text)
        if result.completed and all(c.ok for c in checks):
            self.set_status("Selesai, post-check hijau. Restart untuk memastikan laptop langsung masuk Windows.", "bold green")
        else:
            self.set_status(
                "Ada yang tidak sesuai. Pulihkan lewat menu Pulihkan (p) di dashboard, atau ikuti docs/RECOVERY.md "
                "kalau Windows tidak bisa boot.",
                "bold yellow",
            )

    def _crashed(self, exc: Exception) -> None:
        self.app.stop_signal = None
        self.set_status(
            f"Eksekusi berhenti karena error: {type(exc).__name__}: {exc}. Journal mencatat sesi ini sebagai TERPUTUS; "
            "lihat journal (j) di dashboard, lalu scan ulang dan pakai Pulihkan kalau perlu.",
            "bold red",
        )

    def _finish(self) -> None:
        self.app.stop_signal = None
        self.app.quit_blocker = None
        self.finished = True
        self.set_busy(False)

    def action_request_stop(self, key: str = "Ctrl+C") -> None:
        if self.finished:
            if key == "Esc":
                self.app.back_to_dashboard(rescan=True)
            return
        if not self.executing:
            self.notify("Eksekusi sudah berhenti; post-check sedang berjalan, tunggu sebentar.")
            return
        if not self.stop.requested():
            self.stop.request(f"Dihentikan pengguna ({key})")
            self._write("Berhenti setelah langkah yang sedang jalan selesai…", "bold yellow")
            self.set_status("Berhenti setelah langkah yang sedang jalan selesai…", "bold yellow")

    def action_back(self) -> None:
        self.action_request_stop("Esc")

    def refresh_next(self) -> None:
        self.set_next(self.finished)

    def next(self) -> None:
        self.app.back_to_dashboard(rescan=True)
