"""UninstallFlow (M2c fase A): urutan & syarat dipaksa oleh alur, bukan oleh UI. Laptop simulasi, tanpa sistem asli."""

import argparse
import ast
import threading
import time
from pathlib import Path

import pytest

from distrorelay.windows.journal import incomplete_sessions, read_journal, sessions
from distrorelay.windows.machine import Machine, parse_machine
from distrorelay.windows.preflight import external_volumes
from distrorelay.windows.scanner import run_scan
from distrorelay.windows.stopping import CTRL_C_EVENT, CTRL_CLOSE_EVENT, StopSignal, handle_console_event
from distrorelay.windows.workflow import FlowDeps, FlowError, GateTicket, UninstallFlow, typed
from helpers import fake_mount, laptop_inventory, laptop_world
from test_execute import ARCH, FEDORA, SELECT, preflight_ok

VBOX = Machine("innotek GmbH", "VirtualBox", True)
HP = Machine("HP", "HP Laptop 14s-dq2xxx", False)
SRC = Path(__file__).parent.parent / "src" / "distrorelay"


def make_flow(tmp_path, machine=VBOX, flush_volume=None):
    bcd, esp_root, deps = laptop_world(tmp_path)
    ext = tmp_path / "ext"
    ext.mkdir()
    flow = UninstallFlow(
        FlowDeps(
            scan=lambda progress=None: run_scan(deps(), progress=progress),
            inner_runner=bcd,
            run_preflight=lambda report: preflight_ok(),
            mount_esp=fake_mount(esp_root),
            detect_machine=lambda: machine,
            read_nvram=bcd.nvram,
            flush_bcd=lambda: None,
            flush_volume=flush_volume or (lambda root: None),
            backups_root=tmp_path / "local",
            journal_dir=tmp_path / "journal",
            external_roots=lambda report: [ext],
        )
    )
    return flow, bcd, ext


def ready_for_gate(flow, ext, mode=("vm", {})):
    assert flow.set_mode(*mode) == []
    flow.scan()
    flow.select(SELECT)
    flow.run_preflight()
    flow.confirm("emergency-usb", True)
    backup = flow.make_backup(ext)
    assert backup.ok, [v for v in backup.verifications if not v.ok]
    return backup


def test_full_flow_in_vm(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    gate = flow.gate()
    assert gate.ok, gate.problems
    assert flow.authorize("hapus") is None
    ticket = flow.authorize("HAPUS ")
    assert ticket is not None

    result = flow.execute(ticket)
    assert result.completed, result.stopped
    assert FEDORA not in bcd.entries and ARCH not in bcd.entries
    checks = flow.post_check()
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]

    start = read_journal(flow.journal.path)[0]
    assert start.data["where"] == "vm" and start.data["machine"] == "innotek GmbH VirtualBox"
    assert start.data["plan_hash"] == ticket.plan_hash
    assert sessions(tmp_path / "journal")[0].post_check_ok is True


def test_typed_words_are_exact_except_trailing_spaces():
    assert typed("HAPUS", "HAPUS") and typed("HAPUS ", "HAPUS") and typed("HAPUS\t", "HAPUS")
    for wrong in ("hapus", "Hapus", " HAPUS", "HA PUS", "HAPUSS", "", None):
        assert not typed(wrong, "HAPUS"), wrong


def test_physical_machine_cannot_use_vm_mode(tmp_path):
    flow, bcd, ext = make_flow(tmp_path, machine=HP)
    decision = flow.decide_mode("vm")
    assert decision.forced_real and decision.mode == "asli" and len(decision.statements) == 2
    assert flow.set_mode("vm", {}) == ["vm-scenarios", "recovery-practiced"]

    flow.scan()
    flow.select(SELECT)
    flow.run_preflight()
    flow.confirm("emergency-usb", True)
    flow.make_backup(ext)
    assert any("Mode belum ditetapkan" in p for p in flow.gate().problems)  # mode tidak berlaku -> gerbang menolak

    assert flow.set_mode("vm", {"vm-scenarios": True, "recovery-practiced": True}) == []
    assert flow.mode == "asli"
    assert flow.gate().ok


def test_vm_mode_relaxes_statements_but_never_the_gate(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    assert flow.set_mode("vm", {}) == []
    flow.scan()
    flow.select(SELECT)
    flow.run_preflight()  # konfirmasi USB darurat sengaja tidak diberikan
    flow.make_backup(ext)
    gate = flow.gate()
    assert not gate.ok and any("Belum dikonfirmasi" in p for p in gate.problems)
    with pytest.raises(FlowError, match="Gerbang belum lolos"):
        flow.authorize("HAPUS")


@pytest.mark.parametrize("change", ["rescan", "reselect", "unconfirm", "new-mode", "regate"])
def test_ticket_is_void_after_any_state_change(tmp_path, change):
    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    assert flow.gate().ok
    ticket = flow.authorize("HAPUS")
    {
        "rescan": lambda: flow.scan(),
        "reselect": lambda: flow.select(SELECT),
        "unconfirm": lambda: flow.confirm("emergency-usb", False),
        "new-mode": lambda: flow.set_mode("vm", {}),
        "regate": lambda: flow.gate(),
    }[change]()
    with pytest.raises(FlowError, match="Tiket eksekusi tidak berlaku"):
        flow.execute(ticket)
    assert FEDORA in bcd.entries and ARCH in bcd.entries


def test_ticket_cannot_be_forged_or_reused(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    assert flow.gate().ok
    ticket = flow.authorize("HAPUS")
    forged = GateTicket(**{f: getattr(ticket, f) for f in ticket.__dataclass_fields__})
    with pytest.raises(FlowError):
        flow.execute(forged)
    with pytest.raises(FlowError):
        flow.execute(None)
    assert flow.execute(ticket).completed
    with pytest.raises(FlowError):
        flow.execute(ticket)  # sekali pakai


def test_steps_out_of_order_are_refused(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    with pytest.raises(FlowError):
        flow.select(SELECT)
    flow.scan()
    with pytest.raises(FlowError):
        flow.make_backup(ext)  # preflight dulu
    flow.run_preflight()
    with pytest.raises(FlowError, match="bukan drive eksternal"):
        flow.make_backup(tmp_path / "C")
    with pytest.raises(FlowError, match="Belum lengkap"):
        flow.gate()


def test_unfinished_session_must_be_acknowledged(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    from distrorelay.windows.journal import Journal

    crashed = Journal.start(tmp_path / "journal", "20261022-040607", {"backup": "b1"})
    crashed.record(3, "delete-boot-entry", "nv:0004", "started")
    ready_for_gate(flow, ext)
    assert any("belum diakui" in p for p in flow.gate().problems)
    assert not flow.acknowledge_unfinished("lanjut")
    assert flow.acknowledge_unfinished("LANJUT")
    assert flow.gate().ok


def test_stop_request_finishes_the_current_step_then_stops(tmp_path):
    stop = StopSignal()

    def flush_volume(root):  # dipanggil di akhir langkah 2 (kembalikan fallback)
        stop.request("Dihentikan pengguna (Ctrl+C)")

    flow, bcd, ext = make_flow(tmp_path, flush_volume=flush_volume)
    ready_for_gate(flow, ext)
    assert flow.gate().ok
    result = flow.execute(flow.authorize("HAPUS"), stop=stop)

    assert result.stopped_by_user and not result.completed
    assert [s.kind for s in result.steps] == ["set-windows-first", "restore-fallback"]
    assert "setelah langkah 2 dari" in result.stopped
    assert FEDORA in bcd.entries and bcd.order[0] == "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"

    [summary] = sessions(tmp_path / "journal")
    assert summary.finished and not summary.ok and summary.end_message.startswith("Dihentikan")
    assert [s.path.stem for s in incomplete_sessions(tmp_path / "journal")] == [summary.path.stem]


def test_console_events_request_a_clean_stop():
    stop = StopSignal()
    notes = []
    assert handle_console_event(stop, CTRL_C_EVENT, notes.append)
    assert stop.requested() and notes == ["Berhenti setelah langkah ini selesai…"]

    stop = StopSignal()
    stop.step_started()
    threading.Timer(0.1, stop.step_finished).start()
    threading.Timer(0.3, stop.mark_done).start()  # journal ditutup sesudah langkahnya selesai
    began = time.monotonic()
    assert handle_console_event(stop, CTRL_CLOSE_EVENT, grace=3)
    assert stop.requested() and 0.25 < time.monotonic() - began < 2.5  # menunggu sampai eksekusi benar-benar berhenti

    stop = StopSignal()  # eksekusi yang tidak berhenti juga tidak menahan Windows selamanya
    began = time.monotonic()
    assert handle_console_event(stop, CTRL_CLOSE_EVENT, grace=0.3)
    assert time.monotonic() - began < 1.5


def test_machine_detection_parsing():
    assert parse_machine('{"Manufacturer":"innotek GmbH","Model":"VirtualBox"}').is_vm
    assert parse_machine('{"Manufacturer":"Microsoft Corporation","Model":"Virtual Machine"}').is_vm
    assert parse_machine('{"Manufacturer":"VMware, Inc.","Model":"VMware20,1"}').is_vm
    hp = parse_machine('{"Manufacturer":"HP","Model":"HP Laptop 14s-dq2xxx"}')
    assert not hp.is_vm and hp.description == "HP HP Laptop 14s-dq2xxx"
    assert not parse_machine("bukan json").is_vm  # gagal dikenali = dianggap mesin fisik


def test_second_partition_on_the_windows_ssd_is_never_a_backup_target():
    from distrorelay.windows import disks

    inventory = laptop_inventory()
    inventory.partitions.append(
        disks.Partition(0, 6, "d-part", disks.gpt_types.BASIC_DATA, "Basic data", "basic", 600 * 2**30, 10 * 2**30,
                        "D", ["D:\\"], False, False, False, "Basic", "NTFS", "Data")
    )
    assert external_volumes(inventory) == []


def test_cli_uninstall_runs_on_the_flow(tmp_path, monkeypatch):
    from distrorelay.__main__ import _run_uninstall_command
    from distrorelay.windows.backup import backup_runner, create_backup

    flow, bcd, ext = make_flow(tmp_path)
    report = flow.scan()
    backup = create_backup(
        report, backup_runner(bcd, root=tmp_path / "local"), flow.deps.mount_esp,
        local_root=tmp_path / "local", usb_root=ext,
    )
    answers = iter(["vm", "ya", "HAPUS"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    args = argparse.Namespace(backup=backup.backup_id, select=SELECT)
    assert _run_uninstall_command(args, flow=flow) == 0
    assert FEDORA not in bcd.entries and ARCH not in bcd.entries


def test_cli_refuses_lowercase_confirmation(tmp_path, monkeypatch):
    from distrorelay.__main__ import _run_uninstall_command
    from distrorelay.windows.backup import backup_runner, create_backup

    flow, bcd, ext = make_flow(tmp_path)
    report = flow.scan()
    backup = create_backup(
        report, backup_runner(bcd, root=tmp_path / "local"), flow.deps.mount_esp,
        local_root=tmp_path / "local", usb_root=ext,
    )
    answers = iter(["vm", "ya", "hapus"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert _run_uninstall_command(argparse.Namespace(backup=backup.backup_id, select=SELECT), flow=flow) == 1
    assert FEDORA in bcd.entries


def test_esp_left_mounted_by_a_killed_run_is_found_reused_and_released():
    # Uji VM 7b: proses yang dimatikan paksa meninggalkan ESP ter-mount di Y:, tidak terlihat di Get-Partition.
    from distrorelay.windows.esp import find_mounted_esp, mounted_esp
    from distrorelay.windows.runner import FakeRunner

    esp = next(p for p in laptop_inventory().partitions if p.category == "esp")
    guids = {"C:\\": "aaaa", "E:\\": "bbbb", "Y:\\": esp.guid}
    assert find_mounted_esp(esp, ["C", "E", "Y"], guid_of=guids.get) == "Y"
    assert find_mounted_esp(esp, ["C", "E"], guid_of=guids.get) is None

    fake = FakeRunner()
    fake.add("mountvol Y: /D")
    with mounted_esp(fake, esp, find_existing=lambda part: "Y") as mounted:
        assert mounted.adopted and mounted.letter == "Y"
    assert [list(c) for c in fake.calls] == [["mountvol", "Y:", "/D"]]  # tidak ada /S kedua yang pasti gagal


FORBIDDEN_IN_TUI = {"execute", "runner", "flush", "restore"}


def test_tui_never_imports_executor_or_runner_directly():
    """UI hanya boleh lewat workflow; executor, Runner, flush, dan restore tidak boleh dipanggil langsung."""
    offenders = []
    for path in (SRC / "tui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module] + [f"{module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                if set(name.split(".")) & FORBIDDEN_IN_TUI:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []
