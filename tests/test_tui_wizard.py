"""Wizard TUI (M2c fase B), dijalankan dengan Textual Pilot di laptop simulasi (FakeBcd). Tanpa sistem asli."""

import asyncio
import threading

from textual.widgets import Button, Checkbox, Input, OptionList, RadioButton, SelectionList, Static

from distrorelay.tui.app import DistroRelayApp
from distrorelay.tui.recovery import RecoveryScreen
from distrorelay.tui.sessions import SessionsScreen
from distrorelay.tui.uninstall import (
    BackupScreen,
    CheckTree,
    ConfirmScreen,
    ExecuteScreen,
    ModeScreen,
    PreflightScreen,
    SelectScreen,
    UnfinishedScreen,
)
from distrorelay.windows.journal import Journal, read_journal, sessions
from test_execute import ARCH, FEDORA, SELECT
from test_restore_flow import fedora_entries, uninstalled
from test_workflow import HP, VBOX, make_flow

SIZE = (150, 60)


async def settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def click(pilot, selector: str) -> None:
    await pilot.click(selector)
    await settle(pilot)


def status(app) -> str:
    return str(app.screen.query_one("#status", Static).render())


def next_enabled(app) -> bool:
    return not app.screen.query_one("#next", Button).disabled


async def to_mode(pilot, app) -> None:
    await settle(pilot)  # scan dashboard
    await pilot.press("u")
    await settle(pilot)


async def choose_vm(pilot, app) -> None:
    assert isinstance(app.screen, ModeScreen)
    await click(pilot, "#mode-vm")
    assert next_enabled(app)
    await click(pilot, "#next")


async def check_installations(pilot, app, keys) -> CheckTree:
    assert isinstance(app.screen, SelectScreen)
    tree = app.screen.query_one(CheckTree)
    for node in list(tree.root.children):
        if node.data["inst"].key in keys:
            tree.move_cursor(node)
            await pilot.press("space")
    await pilot.pause()
    return tree


async def through_backup(pilot, app) -> None:
    """Pilih → preflight (konfirmasi USB darurat) → backup baru ke drive eksternal simulasi."""
    await check_installations(pilot, app, SELECT)
    assert next_enabled(app)
    await click(pilot, "#next")

    assert isinstance(app.screen, PreflightScreen)
    assert not next_enabled(app)  # konfirmasi manual belum dicentang
    await click(pilot, "#confirm-emergency-usb")
    assert next_enabled(app)
    await click(pilot, "#next")

    assert isinstance(app.screen, BackupScreen)
    assert not next_enabled(app)
    app.screen.query_one(RadioButton).value = True
    await click(pilot, "#make")
    assert "terverifikasi" in status(app), status(app)
    await click(pilot, "#next")
    assert isinstance(app.screen, ConfirmScreen)


async def type_word(pilot, app, word: str) -> None:
    field = app.screen.query_one("#word", Input)
    field.value = word
    field.focus()
    await pilot.press("enter")
    await pilot.pause()


async def wait_for(event: threading.Event, timeout: float = 10) -> None:
    for _ in range(int(timeout / 0.02)):
        if event.is_set():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("tidak tercapai")


def test_wizard_runs_the_whole_flow_in_a_vm(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await to_mode(pilot, app)
            await choose_vm(pilot, app)
            await settle(pilot)
            await through_backup(pilot, app)
            await settle(pilot)
            assert "Semua syarat terpenuhi" in str(app.screen.query_one("#gate", Static).render())

            await type_word(pilot, app, "hapus")
            assert isinstance(app.screen, ConfirmScreen) and "persis" in status(app)
            assert FEDORA in bcd.entries
            await type_word(pilot, app, "HAPUS")
            assert isinstance(app.screen, ExecuteScreen)
            await settle(pilot)
            assert "post-check hijau" in status(app), status(app)
            assert FEDORA not in bcd.entries and ARCH not in bcd.entries
            [summary] = sessions(tmp_path / "journal")
            assert summary.ok and summary.post_check_ok
            assert read_journal(summary.path)[0].data["where"] == "vm"

            await click(pilot, "#next")
            assert len(app.screen_stack) == 1  # kembali ke dashboard, scan ulang
            await settle(pilot)
            assert app.report is not None

    asyncio.run(scenario())


def _blocking_flow(tmp_path):
    """Laptop simulasi yang tertahan di akhir langkah 2 (flush ESP) sampai test mengizinkan."""
    reached, release = threading.Event(), threading.Event()

    def flush_volume(root):
        reached.set()
        release.wait(10)

    flow, bcd, ext = make_flow(tmp_path, flush_volume=flush_volume)
    return flow, bcd, reached, release


def _stop_mid_execution(tmp_path, key: str):
    flow, bcd, reached, release = _blocking_flow(tmp_path)
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await to_mode(pilot, app)
            await choose_vm(pilot, app)
            await settle(pilot)
            await through_backup(pilot, app)
            await settle(pilot)
            await type_word(pilot, app, "HAPUS")
            await wait_for(reached)
            await pilot.press(key)
            await pilot.pause()
            assert app.is_running and isinstance(app.screen, ExecuteScreen)
            release.set()
            await settle(pilot)
            assert "Dihentikan dengan rapi" in status(app), status(app)
            assert app.screen.flow.execution.stopped_by_user
            assert [s.kind for s in app.screen.flow.execution.steps] == ["set-windows-first", "restore-fallback"]
            assert FEDORA in bcd.entries  # langkah hapus entri belum dijalankan
            return sessions(tmp_path / "journal")

    return asyncio.run(scenario())


def test_ctrl_c_during_execution_finishes_the_step_then_stops(tmp_path):
    [summary] = _stop_mid_execution(tmp_path, "ctrl+c")
    assert summary.finished and not summary.ok and summary.end_message.startswith("Dihentikan pengguna (Ctrl+C)")


def test_quit_during_execution_stops_cleanly_instead_of_exiting(tmp_path):
    [summary] = _stop_mid_execution(tmp_path, "ctrl+q")
    assert summary.end_message.startswith("Dihentikan pengguna (Ctrl+Q)")


def test_wizard_waits_for_the_dashboard_scan(tmp_path):
    # Dua scan bersamaan sama-sama me-mount ESP (mountvol /S), jadi wizard & Pulihkan menunggu scan dashboard.
    flow, bcd, ext = make_flow(tmp_path)
    release = threading.Event()

    def slow_scan(progress=None):
        release.wait(10)
        return flow.deps.scan(progress=progress)

    app = DistroRelayApp(scan=slow_scan, deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            for key in ("u", "p"):
                await pilot.press(key)
                await pilot.pause()
                assert len(app.screen_stack) == 1
            release.set()
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            assert isinstance(app.screen, ModeScreen)

    asyncio.run(scenario())


def test_physical_machine_needs_both_statements(tmp_path):
    flow, bcd, ext = make_flow(tmp_path, machine=HP)
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await to_mode(pilot, app)
            await click(pilot, "#mode-vm")
            assert "bukan VM" in str(app.screen.query_one("#machine", Static).render())
            await click(pilot, "#next")
            assert isinstance(app.screen, ModeScreen) and "Selesaikan dulu di VM" in status(app)
            assert app.screen.flow.mode is None

            for box in app.screen.query(Checkbox):
                box.value = True
            await pilot.pause()
            await pilot.press("ctrl+n")  # pintasan tombol Lanjut
            await settle(pilot)
            assert isinstance(app.screen, SelectScreen) and app.screen.flow.mode == "asli"

    asyncio.run(scenario())


def test_unfinished_session_must_be_acknowledged_first(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    crashed = Journal.start(tmp_path / "journal", "20261022-040607", {"backup": "b1"})
    crashed.record(3, "delete-boot-entry", "nv:0004", "started")
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            banner = app.screen.query_one("#status", Static)
            assert "belum tuntas" in str(banner.render())
            # Uji VM: dashboard 1 baris lebih tinggi dari layar -> tergulung, baris status tertutup.
            assert app.screen.max_scroll_y == 0 and banner.region.y == 1 and banner.region.height >= 2
            await pilot.press("u")
            await settle(pilot)
            assert isinstance(app.screen, UnfinishedScreen)
            await type_word(pilot, app, "lanjut")
            assert isinstance(app.screen, UnfinishedScreen) and "persis" in status(app)
            await type_word(pilot, app, "LANJUT")
            assert isinstance(app.screen, ModeScreen)

    asyncio.run(scenario())


def test_blocked_components_cannot_be_checked(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await to_mode(pilot, app)
            await choose_vm(pilot, app)
            await settle(pilot)
            tree = app.screen.query_one(CheckTree)
            blocked = [n for n in tree._components if n.data["candidate"].blocked]
            assert any(n.data["candidate"].component.kind == "partition" for n in blocked)  # hapus partisi = M3
            for node in blocked:
                tree.move_cursor(node)
                await pilot.press("space")
            assert tree.checked_refs() == [] and not next_enabled(app)
            await pilot.press("ctrl+n")  # Lanjut nonaktif: pintasannya juga tidak berbuat apa-apa
            await pilot.pause()
            assert isinstance(app.screen, SelectScreen)

            await check_installations(pilot, app, ["efi:fedora"])
            refs = tree.checked_refs()
            assert refs and not {n.data["candidate"].component.ref for n in blocked} & set(refs)
            assert next_enabled(app) and app.screen.flow.plan.blocked == []

            await pilot.press("escape")  # kembali ke langkah mode
            await pilot.pause()
            assert isinstance(app.screen, ModeScreen)

    asyncio.run(scenario())


def test_recovery_screen_restores_folder_and_entry(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("p")
            await settle(pilot)
            assert isinstance(app.screen, RecoveryScreen)
            backups = app.screen.query_one("#backups", OptionList)
            backups.focus()
            backups.highlighted = 0
            await pilot.press("enter")
            await settle(pilot)
            assert "lolos verifikasi" in str(app.screen.query_one("#verify", Static).render())

            esp_items = app.screen.query_one("#esp-items", SelectionList)
            entries = app.screen.query_one("#boot-entries", SelectionList)
            windows = next(o for o in entries._options if "Windows Boot Manager" in str(o.prompt))
            assert windows.disabled
            esp_items.select("EFI\\fedora")
            fedora = next(c.key for c in app.screen.flow.entry_choices() if "'Fedora'" in c.label)
            entries.select(fedora)
            await pilot.pause()

            await type_word(pilot, app, "pulihkan")
            assert "persis" in status(app) and not (tmp_path / "esp" / "EFI" / "fedora").exists()
            await type_word(pilot, app, "PULIHKAN")
            await settle(pilot)
            assert "Pemulihan selesai" in status(app), str(app.screen.query_one("#result", Static).render())
            assert (tmp_path / "esp" / "EFI" / "fedora" / "shimx64.efi").exists()
            assert len(fedora_entries(bcd)) == 1

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_journal_screen_lists_sessions(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("j")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            assert "selesai" in str(app.screen.query_one("#journal", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())
