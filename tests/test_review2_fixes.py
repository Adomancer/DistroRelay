"""Test untuk temuan code review 2026-10-23 (tab "Code review" di laporan proyek). Nama test diawali ID temuannya:
B = bug & correctness, P = performa, K = keamanan, Q = kualitas."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import sys
import threading
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input, OptionList

from distrorelay.common import packages as packages_mod
from distrorelay.common import profile as profile_mod
from distrorelay.logging_setup import prune_logs
from distrorelay.tui.app import DistroRelayApp
from distrorelay.tui.sessions import SessionsScreen
from distrorelay.windows import iso as iso_mod
from distrorelay.windows import journal as journal_mod
from distrorelay.windows import nvram
from distrorelay.windows.backup import BackupError, backup_runner, create_backup, verify_backup_dir
from distrorelay.windows.bootrefs import collect_esp_refs
from distrorelay.windows.esp import EspInventory, scan_tree
from distrorelay.windows.iso import DETACHED_ISO, Release, catalog, download, manager
from distrorelay.windows.journal import Journal, incomplete_sessions, sessions
from distrorelay.windows.postcheck import post_check
from distrorelay.windows.restore import restore_esp_items
from distrorelay.windows.scanner import run_scan
from distrorelay.windows.workflow import RESOLVE_WORD, list_backups, mark_session_resolved
from distrorelay_linux import backup as linux_backup
from distrorelay_linux import cli as linux_cli
from distrorelay_linux import dotfiles, pm, provision
from distrorelay_linux.distro import detect
from distrorelay_linux.runner import FakeRunner as LinuxFakeRunner
from helpers import ESP_GUID, fake_mount, file_node, grub_core_image, hd_node, laptop_world, load_option
from test_execute import gate_for, run, world
from test_iso import BIG, TEST_KEYS, _get, fake_release, http_server, make_tools, needs_gpgv  # noqa: F401
from test_linux_backup import FEDORA, run_backup
from test_linux_backup import make_runner as linux_runner
from test_linux_kit import OS_RELEASE
from test_linux_kit import export as export_kit_for_test
from test_tui_wizard import SIZE, settle
from test_workflow import make_flow

USB_DISK = {
    "Number": 1, "Guid": "{11111111-2222-3333-4444-555555555555}", "FriendlyName": "WD Elements 25A2",
    "SerialNumber": "x", "Size": 1000204886016, "PartitionStyle": "GPT", "LogicalSectorSize": 512, "BusType": "USB",
    "IsBoot": False, "IsSystem": False,
}
USB_PARTITION = {
    "DiskNumber": 1, "PartitionNumber": 1, "Guid": "{66666666-7777-8888-9999-000000000000}",
    "GptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}", "Offset": 1048576, "Size": 1000000000000,
    "DriveLetter": "E", "AccessPaths": ["E:\\"], "IsBoot": False, "IsSystem": False, "IsHidden": False,
    "Type": "Basic", "FileSystem": "NTFS", "FileSystemLabel": "Backup",
}


def _plug_usb_first(bcd) -> None:
    """Disk USB dicolok dan mendapat nomor 0, jadi SSD Windows bergeser jadi disk 1."""
    for disk in bcd.disks["Disks"]:
        disk["Number"] += 1
    for part in bcd.disks["Partitions"]:
        part["DiskNumber"] += 1
    bcd.disks["Disks"].insert(0, dict(USB_DISK, Number=0))
    bcd.disks["Partitions"].insert(0, dict(USB_PARTITION, DiskNumber=0))


# --- B-1: hasil verifikasi saat backup dibuat wajib hijau ---------------------------------


def _set_creation_checks(backup, tmp_path, checks) -> None:
    path = tmp_path / "local" / backup.backup_id / "verification.json"
    if checks is None:
        path.unlink()
    else:
        path.write_text(json.dumps(checks), encoding="utf-8")


def test_b1_gate_refuses_a_backup_that_failed_verification_when_created(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    assert gate_for(tmp_path, plan, report, backup).ok
    _set_creation_checks(backup, tmp_path, [{"name": "BCD store bisa dibaca", "ok": False, "message": "Gagal"},
                                            {"name": "Hash file backup lokal", "ok": True, "message": "ok"}])
    gate = gate_for(tmp_path, plan, report, backup)
    assert any("gagal verifikasi saat dibuat (BCD store bisa dibaca" in p for p in gate.problems), gate.problems


def test_b1_gate_refuses_a_backup_without_verification_json(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    _set_creation_checks(backup, tmp_path, None)
    gate = gate_for(tmp_path, plan, report, backup)
    assert any("verification.json" in p for p in gate.problems), gate.problems


def test_b1_backup_list_marks_failed_backups_as_unusable(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    [info] = list_backups(tmp_path / "local")
    assert info.verified is True and info.usable
    _set_creation_checks(backup, tmp_path, [{"name": "Salinan USB", "ok": False, "message": "Gagal menulis"}])
    [info] = list_backups(tmp_path / "local")
    assert info.verified is False and not info.usable
    assert "GAGAL verifikasi" in info.describe()


# --- B-3: manifest rusak tidak membuat gerbang crash ---------------------------------------


def test_b3_gate_reports_a_corrupt_manifest_instead_of_crashing(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    (tmp_path / "local" / backup.backup_id / "manifest.json").write_text("{ rusak", encoding="utf-8")
    gate = gate_for(tmp_path, plan, report, backup)
    assert not gate.ok and gate.manifest is None
    assert gate.problems[-1].startswith("Backup lokal tidak lolos verifikasi: manifest.json")


def test_b3_manifest_that_is_not_an_object_fails_verification(tmp_path):
    directory = tmp_path / "20261023-101500"
    directory.mkdir()
    for content in ("[]", '{"files": [{"nama": "x"}]}'):
        (directory / "manifest.json").write_text(content, encoding="utf-8")
        [check] = verify_backup_dir(directory)
        assert check.name == "manifest.json" and not check.ok
    (directory / "manifest.json").write_text("[]", encoding="utf-8")
    assert list_backups(tmp_path) == []


# --- B-2: hanya disk Windows + disk yang diubah rencana yang dibandingkan --------------------


def test_b2_plugging_a_usb_disk_does_not_void_the_backup(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    _plug_usb_first(bcd)
    now = run_scan(deps())
    assert now.inventory.fingerprint() != report.inventory.fingerprint()  # cara lama: backup dianggap basi
    gate = gate_for(tmp_path, plan, now, backup)
    assert gate.ok, gate.problems


def test_b2_change_on_the_windows_disk_is_still_refused_and_named(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    bcd.disks["Partitions"][-1]["Size"] -= 1024 * 1024  # partisi #5 di SSD Windows berubah ukuran
    gate = gate_for(tmp_path, plan, run_scan(deps()), backup)
    assert any("Layout disk berubah" in p and "SK hynix" in p for p in gate.problems), gate.problems


def test_b2_post_check_ignores_a_usb_disk_plugged_after_the_scan(tmp_path):
    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    result, _ = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert result.completed, result.stopped
    _plug_usb_first(bcd)
    checks = post_check(plan, gate.manifest, report, run_scan(deps()))
    layout = next(c for c in checks if c.name.startswith("Layout disk"))
    assert layout.ok, layout.message


# --- B-5: sesi lama bisa dituntaskan tanpa uninstall ulang -----------------------------------


FEDORA_DIR, ARCH_DIR = "EFI\\fedora", "EFI\\Arch"


def _cut_uninstall(directory, *extra_steps):
    """Uninstall yang berhenti gagal sesudah menghapus folder Fedora dan entrinya."""
    cut = Journal.start(directory, "20261022-100000", {"kind": "uninstall", "backup": "20261022-090000"})
    steps = [("set-windows-first", None), ("delete-boot-entry", "nv:0003"), ("delete-esp-item", FEDORA_DIR),
             *extra_steps]
    for number, (kind, ref) in enumerate(steps, 1):
        cut.record(number, kind, ref, "started")
        cut.record(number, kind, ref, "done")
    cut.record(len(steps) + 1, "delete-esp-item", ARCH_DIR, "started")
    cut.record(len(steps) + 1, "delete-esp-item", ARCH_DIR, "failed", "berubah sejak backup")
    cut.finish(False, "Berhenti di langkah terakhir")
    return cut


def _restore(directory, name, esp_items, entries, partitions=(), ok=True):
    session = Journal.start(directory, name, {"kind": "restore", "backup": "20261022-090000", "esp_items": esp_items,
                                              "entries": entries, "partitions": list(partitions)})
    session.post_check(ok, [] if ok else ["x"])
    session.finish(ok)


def test_b5_restore_that_covers_every_deleted_item_resolves_the_uninstall(tmp_path):
    _cut_uninstall(tmp_path)
    _restore(tmp_path, "20261022-110000", [FEDORA_DIR], ["Boot0003"])  # Arch gagal dihapus di tengah, belum pulih
    assert [s.path.stem for s in incomplete_sessions(tmp_path)] == ["20261022-100000"]
    _restore(tmp_path, "20261022-120000", [FEDORA_DIR, ARCH_DIR], ["Boot0003"])
    first = sessions(tmp_path)[0]
    assert (first.resolved_by, first.resolved_how) == ("20261022-120000", "pemulihan")
    assert incomplete_sessions(tmp_path) == []


def test_b5_failed_restore_or_missing_entry_does_not_resolve(tmp_path):
    _cut_uninstall(tmp_path)
    _restore(tmp_path, "20261022-110000", [FEDORA_DIR, ARCH_DIR], [])  # entri Boot0003 tidak dipulihkan
    _restore(tmp_path, "20261022-120000", [FEDORA_DIR, ARCH_DIR], ["Boot0003"], ok=False)
    # Pemulihan yang gagal juga butuh perhatian; yang penting uninstall-nya belum dianggap tuntas.
    assert [s.path.stem for s in incomplete_sessions(tmp_path)] == ["20261022-100000", "20261022-120000"]


def test_b5_extended_c_can_only_be_resolved_by_hand(tmp_path):
    _cut_uninstall(tmp_path, ("delete-partition", "disk0-p5"), ("extend-c", "disk0-p3"))
    _restore(tmp_path, "20261022-110000", [FEDORA_DIR, ARCH_DIR], ["Boot0003"], ["disk0-p5"])
    assert len(incomplete_sessions(tmp_path)) == 1
    assert mark_session_resolved(tmp_path, "20261022-100000", "tuntas", "x") is not None  # huruf kecil ditolak
    assert len(incomplete_sessions(tmp_path)) == 1
    assert mark_session_resolved(tmp_path, "20261022-100000", RESOLVE_WORD, "dicek manual") is None
    first = sessions(tmp_path)[0]
    assert first.resolved_how == "manual" and first.marked_resolved.message == "dicek manual"
    assert incomplete_sessions(tmp_path) == []
    assert mark_session_resolved(tmp_path, "20261022-100000", RESOLVE_WORD, "x") is not None  # sudah tidak perlu


def test_b5_cli_marks_a_session_resolved(tmp_path, monkeypatch):
    from distrorelay.__main__ import _run_journal_command

    _cut_uninstall(tmp_path)
    monkeypatch.setattr("distrorelay.__main__._ask_word", lambda prompt: "TUNTAS")
    assert _run_journal_command(SimpleNamespace(tuntaskan="20261022-100000"), journal_root=tmp_path) == 0
    assert incomplete_sessions(tmp_path) == []
    assert _run_journal_command(SimpleNamespace(tuntaskan="20261022-999999"), journal_root=tmp_path) == 1


def test_b5_tui_journal_screen_marks_a_session_resolved(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    _cut_uninstall(tmp_path / "journal")
    app = DistroRelayApp(deps=flow.deps)

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("j")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)
            options = app.screen.query_one("#attention", OptionList)
            assert options.option_count == 1
            options.highlighted = 0
            options.action_select()
            await pilot.pause()
            word = app.screen.query_one("#resolve-word", Input)
            assert not word.disabled and word.has_focus
            await pilot.press(*"TUNTAS", "enter")
            await pilot.pause()
            assert incomplete_sessions(tmp_path / "journal") == []
            assert app.screen.query_one("#attention", OptionList).option_count == 0

    asyncio.run(scenario())


# --- P-1: backup Linux dibaca streaming, dan tidak di thread UI ---------------------------------


def test_p1_latest_backup_stops_at_the_newest_valid_one(tmp_path, monkeypatch):
    kit, older = run_backup(tmp_path)
    newer = linux_backup.create_backup(kit, linux_runner(), FEDORA, home=tmp_path / "home", curate=lambda c: [],
                                       now=datetime(2026, 10, 23, 9, 0, 0))
    checked = []
    real = linux_backup.verify
    monkeypatch.setattr(linux_backup, "verify", lambda path, **kw: checked.append(Path(path).name) or real(path, **kw))
    assert linux_backup.latest_backup(kit.backups_dir) == newer.path
    assert checked == [newer.path.name]  # backup yang lebih lama tidak dibaca


def test_p1_quick_check_sees_missing_or_resized_files_without_reading_them(tmp_path):
    kit, result = run_backup(tmp_path)
    archive = result.path / linux_backup.DOTFILES
    data = archive.read_bytes()
    archive.write_bytes(bytes([data[0] ^ 1]) + data[1:])  # isi berubah, ukuran sama
    assert linux_backup.verify(result.path, hashes=False) == []
    assert "hash tidak cocok" in linux_backup.verify(result.path)[0]
    archive.write_bytes(data + b"x")
    assert "ukurannya berubah" in linux_backup.verify(result.path, hashes=False)[0]


def test_p1_dotfiles_archive_has_a_total_size_limit(tmp_path):
    home = tmp_path / "home"
    (home / "a").mkdir(parents=True)
    (home / "a" / "1.bin").write_bytes(b"x" * 600)
    (home / "a" / "2.bin").write_bytes(b"y" * 600)
    report = dotfiles.write_archive(io.BytesIO(), home, ["a"], max_total_size=1000)
    assert report.files == ["a/1.bin"]
    assert [(rel, "batas" in why) for rel, why in report.skipped] == [("a/2.bin", True)]


def test_p1_flow_checks_linux_backups_during_scan_not_while_selecting(tmp_path, monkeypatch):
    flow, bcd, ext = make_flow(tmp_path)
    calls = []
    real = linux_backup.list_backups
    monkeypatch.setattr(linux_backup, "list_backups", lambda path, **kw: calls.append(kw) or real(path, **kw))
    flow.scan()
    after_scan = len(calls)
    assert after_scan >= 1 and all(kw == {"hashes": False} for kw in calls)
    flow.select(["efi:fedora"])
    flow.select(["efi:fedora", "efi:arch"])
    assert len(calls) == after_scan


# --- P-2: waktu baca NVRAM tercatat ---------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="read_nvram hanya untuk Windows")
def test_p2_nvram_read_logs_how_many_variables_and_how_long(monkeypatch, caplog):
    option = load_option("Fedora", [hd_node(1, ESP_GUID), file_node("\\EFI\\fedora\\shimx64.efi")], b"")
    values = {"BootOrder": bytes([1, 0]), "Boot0001": option}
    monkeypatch.setattr(nvram, "_enable_privilege", lambda name: None)
    monkeypatch.setattr(nvram, "_get_variable", lambda name, missing_ok=False: values.get(name))
    with caplog.at_level(logging.INFO, logger="distrorelay.windows.nvram"):
        snapshot = nvram.read_nvram()
    assert list(snapshot.options) == [1]
    assert "NVRAM: 259 variabel diminta (1 Boot#### ada)" in caplog.text


# --- P-3 ada di tests/test_shrink.py (query post-check ringan) -------------------------------------
# --- P-4: feed ISO diambil bersamaan ---------------------------------------------------------------


def _slow_source(name: str, barrier: threading.Barrier):
    def latest(fetch, edition):
        barrier.wait()  # kalau diambil berurutan, distro kedua belum mulai dan ini timeout
        url = f"https://example.org/{name}-1.iso"
        return Release(name, "1", "x", f"{name}-1.iso", url, DETACHED_ISO, signature_url=url + ".sig", sha256="0" * 64)

    return SimpleNamespace(NAME=name.title(), latest=latest, LOCAL_PATTERN=re.compile("^$"), DEFAULT_EDITION="x")


def test_p4_feeds_are_fetched_at_the_same_time_and_rows_keep_their_order(tmp_path):
    barrier = threading.Barrier(2, timeout=5)
    sources = {"zeta": _slow_source("zeta", barrier), "alfa": _slow_source("alfa", barrier)}
    waiting = []
    rows = catalog.check(sources, fetch=None, downloads_root=tmp_path, progress=waiting.append)
    assert [r.distro for r in rows] == ["zeta", "alfa"]
    assert all(r.latest is not None for r in rows), [r.error for r in rows]
    assert len(waiting) == 1 and len(waiting[0]) == 1


# --- P-5: ISO tidak dibaca ulang tanpa perlu ---------------------------------------------------------


def test_p5_download_reports_sha256_including_a_resumed_part(http_server, tmp_path):
    http_server.files["/x.iso"] = BIG
    (tmp_path / "x.iso.part").write_bytes(BIG[:1_000_000])
    seen = []
    _get(http_server, tmp_path / "x.iso", on_sha256=seen.append)
    assert seen == [hashlib.sha256(BIG).hexdigest()]


@needs_gpgv
def test_p5_arch_copy_reuses_the_recorded_gpgv_result_but_still_checks_sha256(tmp_path):
    tools = make_tools(tmp_path)
    runs = []
    real_run = tools.run_gpgv
    tools.run_gpgv = lambda args, cwd: runs.append(args) or real_run(args, cwd)
    assert manager.prepare(fake_release(DETACHED_ISO), TEST_KEYS, tools).ok
    saved = iso_mod.saved_releases("fake", tools.root)[0]
    usb = tmp_path / "usb"
    usb.mkdir()
    before = len(runs)
    steps = manager.copy_to_drive(saved, TEST_KEYS, tools, usb)
    assert all(s.ok for s in steps), steps
    assert len(runs) == before  # gpgv tidak membaca seluruh ISO lagi
    (usb / saved.release.iso_name).unlink()
    data = bytearray(saved.iso_path.read_bytes())
    data[10] ^= 1  # ISO berubah sesudah lolos verifikasi
    saved.iso_path.write_bytes(bytes(data))
    steps = manager.copy_to_drive(saved, TEST_KEYS, tools, usb)
    assert not all(s.ok for s in steps) and not list(usb.iterdir())


# --- P-6: journal di-cache, log lama dihapus ----------------------------------------------------------


def test_p6_unchanged_journals_are_not_parsed_again(tmp_path, monkeypatch):
    _cut_uninstall(tmp_path)
    sessions(tmp_path)
    parsed = []
    real = journal_mod.summarize
    monkeypatch.setattr(journal_mod, "summarize", lambda path: parsed.append(path) or real(path))
    sessions(tmp_path)
    assert parsed == []
    Journal(sorted(tmp_path.glob("*.jsonl"))[0]).mark_resolved("x")
    assert sessions(tmp_path)[0].resolved_how == "manual" and len(parsed) == 1


def test_p6_old_daily_logs_are_pruned(tmp_path):
    for day in ("2026-08-01", "2026-09-30", "2026-10-23"):
        (tmp_path / f"distrorelay-{day}.log").write_text("x")
    (tmp_path / "lain.log").write_text("x")
    removed = prune_logs(tmp_path, date(2026, 10, 23))
    assert [p.name for p in removed] == ["distrorelay-2026-08-01.log"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "distrorelay-2026-09-30.log", "distrorelay-2026-10-23.log", "lain.log"]


# --- P-7: Arch sinkron sekali, lalu per paket tanpa -Syu ------------------------------------------------


def test_p7_arch_fallback_syncs_once_then_installs_package_by_package():
    backend = pm.backend_for("arch")
    step = provision.Step("native", "Pasang paket", [backend.install(["git", "vlc"], False)], each=["git", "vlc"],
                          each_command=lambda names: backend.install_one(names, False),
                          each_prepare=backend.prepare_each(False))
    runner = LinuxFakeRunner()
    runner.add("sudo pacman -Syu --needed --noconfirm git vlc", 1)
    runner.add("sudo pacman -S --needed --noconfirm vlc", 1)
    result = provision._run_step(step, runner)
    assert result.status == "sebagian" and result.message == "gagal: vlc"
    assert runner.ran() == [
        "sudo pacman -Syu --needed --noconfirm git vlc",
        "sudo pacman -Syu --noconfirm",
        "sudo pacman -S --needed --noconfirm git",
        "sudo pacman -S --needed --noconfirm vlc",
    ]
