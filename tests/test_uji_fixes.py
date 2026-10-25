"""Test untuk temuan uji VM 24-25 Oktober dan uji laptop 25 Oktober 2026 (tab "Plan perbaikan temuan uji" di laporan
proyek). Nama test diawali ID temuannya: T = temuan, C = catatan kecil."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from distrorelay.windows import backup as backup_mod
from distrorelay.windows import iso as iso_mod
from distrorelay.windows.iso import DETACHED_CHECKSUM
from distrorelay_linux import backup as linux_backup
from distrorelay_linux.distro import detect
from distrorelay_linux.runner import FakeRunner as LinuxFakeRunner
from distrorelay.windows.backup import FLUSH_CHECK, backup_runner, create_backup, creation_checks, verify_backup_dir
from distrorelay.windows.execute import check_gate
from distrorelay.windows.flush import FlushError
from distrorelay.windows.scanner import run_scan
from distrorelay.windows.uninstall_plan import build_plan, expand_selection
from distrorelay.windows.workflow import list_backups
from helpers import fake_mount, laptop_world
from test_execute import SELECT, preflight_ok
from test_linux_backup import make_home, make_kit
from test_linux_backup import make_runner as make_linux_runner
from test_linux_kit import OS_RELEASE
from test_iso import FAKE_ISO, TEST_KEYS, fake_release, needs_gpgv
from test_iso import make_tools as make_iso_tools
from test_shrink import run_cli as run_shrink_cli
from test_workflow import make_flow, ready_for_gate


def _world(tmp_path, **backup_kwargs):
    bcd, esp_root, deps = laptop_world(tmp_path)
    report = run_scan(deps())
    refs, unknown = expand_selection(report.analysis, SELECT)
    assert unknown == []
    plan = build_plan(report.analysis, report.boot_entries, refs, inventory=report.inventory, fsinfo=report.fsinfo)
    result = create_backup(
        report, backup_runner(bcd, root=tmp_path / "local"), fake_mount(esp_root),
        local_root=tmp_path / "local", usb_root=tmp_path / "ext", **backup_kwargs,
    )
    return bcd, esp_root, deps, report, plan, result


def _gate(tmp_path, plan, report, result):
    return check_gate(
        plan, report, preflight_ok(), backup_id=result.backup_id, local_root=tmp_path / "local",
        external_roots=[tmp_path / "ext"], confirmed={"emergency-usb"},
    )


# --- T-6: salinan backup eksternal di-flush sebelum diverifikasi ---------------------------------


def test_t6_usb_copy_is_fsynced_and_volume_flushed_before_uncached_verification(tmp_path, monkeypatch):
    events = []
    real_fsync, real_uncached = os.fsync, backup_mod.sha256_uncached
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(backup_mod, "sha256_uncached", lambda path, *a: (events.append("baca"), real_uncached(path, *a))[1])
    # Di test, tujuan USB adalah folder temp; di aslinya root drive (E:\), dan hanya root drive yang di-flush volumenya.
    monkeypatch.setattr(backup_mod, "_is_drive_root", lambda path: Path(path) == tmp_path / "ext")

    *_, result = _world(tmp_path, flush=lambda root: events.append(("flush", Path(root))))

    assert result.ok, [v for v in result.verifications if not v.ok]
    usb_files = [p for p in Path(result.usb_dir).rglob("*") if p.is_file()]
    assert events.count("fsync") >= len(usb_files) > 0
    flush_at = events.index(("flush", tmp_path / "ext"))
    # fsync per file + flush volume dulu, baru dibaca ulang langsung dari perangkat.
    assert "baca" not in events[:flush_at] and "baca" in events[flush_at:]
    check = next(v for v in result.verifications if v.name == FLUSH_CHECK)
    assert check.ok and f"{len(usb_files)} file" in check.message


def test_t6_failed_flush_fails_the_backup_and_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_mod, "_is_drive_root", lambda path: Path(path) == tmp_path / "ext")

    def broken(root):
        raise FlushError("Volume E: gagal ditulis ke disk (WinError 1117)")

    _, _, _, report, plan, result = _world(tmp_path, flush=broken)

    assert not result.ok
    failed = [v for v in result.verifications if not v.ok]
    assert [v.name for v in failed] == [FLUSH_CHECK]
    assert "WinError 1117" in failed[0].message and "Buat backup baru" in failed[0].message
    saved = creation_checks(Path(result.local_dir))
    assert saved is not None and not next(v for v in saved if v.name == FLUSH_CHECK).ok
    gate = _gate(tmp_path, plan, report, result)
    assert not gate.ok
    assert any("gagal verifikasi saat dibuat" in p and FLUSH_CHECK in p for p in gate.problems)


def test_t6_folder_target_is_fsynced_without_volume_flush(tmp_path):
    """Tujuan yang bukan root drive (mis. folder temp di test) tidak di-flush volumenya, tapi file tetap di-fsync."""
    calls = []
    *_, result = _world(tmp_path, flush=calls.append)
    assert result.ok
    assert calls == []
    assert next(v for v in result.verifications if v.name == FLUSH_CHECK).ok


def test_t6_gate_message_for_broken_external_copy_tells_what_to_do(tmp_path):
    _, _, _, report, plan, result = _world(tmp_path)
    (Path(result.usb_dir) / "fsinfo.json").write_text("{}", encoding="utf-8")  # seperti salinan yang rusak di CR-6
    gate = _gate(tmp_path, plan, report, result)
    problem = next(p for p in gate.problems if p.startswith("Salinan eksternal tidak lolos verifikasi"))
    assert "fsinfo.json" in problem and "Safely Remove" in problem


def test_t6_backup_text_reminds_to_use_safely_remove(tmp_path):
    from distrorelay import render

    *_, result = _world(tmp_path)
    assert "Safely Remove" in render.backup_text(result).plain


# --- T-4: bootstat.dat (ditulis Windows tiap boot) tidak membatalkan backup ------------------------


def _reboot_and_gate(tmp_path, change):
    """Backup, lalu ESP berubah (seperti sesudah reboot), scan ulang, gerbang dengan backup yang sama."""
    _, esp_root, deps, _, plan, result = _world(tmp_path)
    assert result.ok
    change(esp_root)
    return _gate(tmp_path, plan, run_scan(deps()), result)


def _write_bootstat(esp_root):
    (esp_root / "EFI" / "Microsoft" / "Boot" / "bootstat.dat").write_bytes(os.urandom(64))


def test_t4_bootstat_written_by_windows_boot_is_only_a_note(tmp_path):
    gate = _reboot_and_gate(tmp_path, _write_bootstat)
    assert gate.ok, gate.problems
    assert gate.notes == ["efi\\microsoft\\boot\\bootstat.dat berubah sejak backup (ditulis Windows setiap boot), diabaikan"]


def test_t4_other_esp_changes_still_reject_the_backup(tmp_path):
    def change(esp_root):
        _write_bootstat(esp_root)
        (esp_root / "EFI" / "fedora" / "grub.cfg").write_text("search --fs-uuid lain\n", encoding="utf-8")

    gate = _reboot_and_gate(tmp_path, change)
    assert any("Isi ESP berubah" in p and "efi\\fedora\\grub.cfg" in p for p in gate.problems)


def test_t4_windows_boot_manager_change_still_rejects_the_backup(tmp_path):
    gate = _reboot_and_gate(
        tmp_path, lambda esp_root: (esp_root / "EFI" / "Microsoft" / "Boot" / "bootmgfw.efi").write_bytes(b"MZ lain")
    )
    assert any("Isi ESP berubah" in p and "bootmgfw.efi" in p for p in gate.problems)


def test_t4_reboot_between_backup_and_execution_keeps_post_check_green(tmp_path):
    """Uji VM U-4 2026-10-25: gerbang sudah mengabaikan bootstat.dat, tapi post-check masih menandai
    "File ESP lain tidak tersentuh" gagal, jadi sesi yang sebenarnya sukses tercatat butuh perhatian."""
    from distrorelay.windows.postcheck import post_check
    from test_execute import gate_for, run, world

    bcd, esp_root, deps, _, plan, backup = world(tmp_path)
    _write_bootstat(esp_root)  # reboot sesudah backup
    report = run_scan(deps())
    gate = gate_for(tmp_path, plan, report, backup)
    assert gate.ok, gate.problems
    result, _ = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert result.completed, result.stopped
    _write_bootstat(esp_root)  # dan Windows menulisnya lagi sebelum post-check
    checks = post_check(plan, gate.manifest, report, run_scan(deps()))
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_t4_post_check_still_catches_other_esp_changes(tmp_path):
    from distrorelay.windows.postcheck import post_check
    from test_execute import gate_for, run, world

    bcd, esp_root, deps, report, plan, backup = world(tmp_path)
    gate = gate_for(tmp_path, plan, report, backup)
    result, _ = run(tmp_path, bcd, esp_root, plan, gate.manifest)
    assert result.completed
    (esp_root / "EFI" / "HP" / "BIOS" / "Current" / "08A4F.bin").write_bytes(b"diubah")
    failed = [c for c in post_check(plan, gate.manifest, report, run_scan(deps())) if not c.ok]
    assert [c.name for c in failed] == ["File ESP lain tidak tersentuh"] and "08a4f.bin" in failed[0].message


# --- T-1: shrink-c yang ditolak tidak mencetak angka negatif -------------------------------------


def test_t1_rejected_shrink_prints_only_the_reasons(monkeypatch, tmp_path, capsys):
    code, _ = run_shrink_cli(monkeypatch, tmp_path, 9999, [])
    out = capsys.readouterr().out
    assert code == 1 and "[GAGAL]" in out
    assert "Dikecilkan" not in out and " -" not in out.replace("--size", "")  # dulu: "C: jadi -10630462439424 B"


# --- T-2: file JSON backup yang disimpan ulang dengan BOM tetap terbaca --------------------------


def _add_bom(path: Path) -> None:
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())  # seperti PowerShell 5.1 Set-Content -Encoding utf8


def test_t2_verification_json_with_bom_reports_the_failed_check(tmp_path):
    _, _, _, report, plan, result = _world(tmp_path)
    local = Path(result.local_dir)
    data = json.loads((local / "verification.json").read_text(encoding="utf-8"))
    data[0]["ok"] = False
    (local / "verification.json").write_text(json.dumps(data), encoding="utf-8")
    _add_bom(local / "verification.json")

    saved = creation_checks(local)
    assert saved is not None and not saved[0].ok  # dulu: None = "tidak tercatat"
    assert "[GAGAL verifikasi saat dibuat]" in next(b for b in list_backups(tmp_path / "local")).describe()
    gate = _gate(tmp_path, plan, report, result)
    assert any("gagal verifikasi saat dibuat" in p for p in gate.problems)
    assert not any("tidak ada atau rusak" in p for p in gate.problems)


def test_t2_manifest_with_bom_still_verifies(tmp_path):
    *_, result = _world(tmp_path)
    _add_bom(Path(result.local_dir) / "manifest.json")
    assert all(v.ok for v in verify_backup_dir(Path(result.local_dir)))


# --- T-5: --select lama sesudah crash diberi daftar kandidat yang tersisa ------------------------


def test_t5_unknown_selector_after_crash_lists_what_is_left(tmp_path, monkeypatch, capsys):
    import argparse

    from distrorelay.cli.uninstall import cmd_uninstall
    from distrorelay.windows.journal import Journal

    flow, _, _ = make_flow(tmp_path)
    crashed = Journal.start(tmp_path / "journal", "20261024-222811", {"backup": "20261024-222811"})
    crashed.record(3, "delete-esp-item", "EFI\\fedora", "started")  # terputus di tengah langkah ini
    answers = iter(["LANJUT", "vm", "ya"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    code = cmd_uninstall(argparse.Namespace(backup="20261024-222811", select=["efi:hilang"]), flow=flow)

    out = capsys.readouterr().out
    assert code == 1
    assert "Tidak dikenal: efi:hilang" in out
    assert "Mungkin sudah dihapus oleh sesi 20261024-222811" in out
    assert "Kandidat yang bisa dipilih" in out and "efi:arch" in out and "disk0-p5" in out


def test_t5_unknown_selector_without_unfinished_session_still_lists_candidates(tmp_path, monkeypatch, capsys):
    import argparse

    from distrorelay.cli.uninstall import cmd_uninstall

    flow, _, _ = make_flow(tmp_path)
    answers = iter(["vm", "ya"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cmd_uninstall(argparse.Namespace(backup="20261024-222811", select=["efi:hilang"]), flow=flow) == 1
    out = capsys.readouterr().out
    assert "Mungkin sudah dihapus" not in out and "Kandidat yang bisa dipilih" in out


# --- C-1: pesan layout berubah menyebut partisi baru, bukan hanya drive eksternal ----------------


def test_c1_layout_change_message_mentions_new_partitions():
    from distrorelay.windows.execute import ExecutionError, _Context

    ctx = _Context.__new__(_Context)

    class Inventory:
        def layout(self):
            return {("disk", "{baru}", "tipe", 1024, 100)}

    with pytest.raises(ExecutionError) as caught:
        _Context.check_layout(ctx, Inventory(), set(), "sebelum partisi dihapus")
    assert "Disk Management" in str(caught.value) and "buat rencana baru" in str(caught.value)


# --- C-2: sesi uninstall gagal hanya dituntaskan oleh sesi yang menghapus sisanya -----------------


def _uninstall_session(directory, name, planned, done, *, ok, failed=()):
    from distrorelay.windows.journal import Journal

    data = {"kind": "uninstall", "backup": "20261024-235000"}
    if planned is not None:
        data["planned"] = [list(item) for item in planned]
    journal = Journal.start(directory, name, data)
    step = 0
    for kind, ref in done:
        step += 1
        journal.record(step, kind, ref, "started")
        journal.record(step, kind, ref, "done")
    for kind, ref in failed:
        step += 1
        journal.record(step, kind, ref, "started")
        journal.record(step, kind, ref, "failed", "Layout disk sebelum partisi dihapus tidak sama")
    journal.finish(ok)
    journal.post_check(ok, [] if ok else ["Partisi yang dihapus sudah tidak ada"])


P6, P7 = ("partition", "disk0-p6"), ("partition", "disk0-p7")


def test_c2_failed_session_is_not_settled_by_a_success_that_removed_something_else(tmp_path):
    from distrorelay.windows.journal import incomplete_sessions, sessions

    _uninstall_session(tmp_path, "20261024-235559", [("esp", "efi\\fedora"), P6], [("delete-esp-item", "EFI\\fedora")],
                       ok=False, failed=[("delete-partition", "disk0-p6")])
    _uninstall_session(tmp_path, "20261025-001012", [P7], [("delete-partition", "disk0-p7")], ok=True)

    failed = sessions(tmp_path)[0]
    assert failed.pending == frozenset({P6})
    assert [s.path.stem for s in incomplete_sessions(tmp_path)] == ["20261024-235559"]


def test_c2_failed_session_is_settled_by_a_success_that_removed_the_rest(tmp_path):
    from distrorelay.windows.journal import incomplete_sessions, sessions

    _uninstall_session(tmp_path, "20261024-235559", [("esp", "efi\\fedora"), P6], [("delete-esp-item", "EFI\\fedora")],
                       ok=False, failed=[("delete-partition", "disk0-p6")])
    _uninstall_session(tmp_path, "20261025-001012", [P6], [("delete-partition", "disk0-p6")], ok=True)

    assert incomplete_sessions(tmp_path) == []
    assert sessions(tmp_path)[0].resolved_by == "20261025-001012"


def test_c2_old_journal_without_plan_keeps_the_old_rule(tmp_path):
    from distrorelay.windows.journal import incomplete_sessions

    _uninstall_session(tmp_path, "20261024-235559", None, [], ok=False, failed=[("delete-partition", "disk0-p6")])
    _uninstall_session(tmp_path, "20261025-001012", None, [("delete-partition", "disk0-p7")], ok=True)
    assert incomplete_sessions(tmp_path) == []


def test_c2_workflow_records_the_planned_items(tmp_path, monkeypatch):
    from distrorelay.windows.journal import read_journal

    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    gate = flow.gate()
    assert gate.ok, gate.problems
    flow.execute(flow.authorize("HAPUS"))
    start = next(e for e in read_journal(next((tmp_path / "journal").glob("*.jsonl"))) if e.status == "session-start")
    assert ["esp", "efi\\fedora"] in start.data["planned"] and ["entry", "boot0003"] in start.data["planned"]


# --- C-3: setup menyebut backup terbaru yang rusak -------------------------------------------------


def _setup_cli(tmp_path, monkeypatch):
    from distrorelay_linux import cli as linux_cli

    kit = make_kit(tmp_path / "usb")
    home = make_home(tmp_path / "home")
    runner = make_linux_runner()
    fedora = detect(OS_RELEASE["fedora"])
    monkeypatch.setattr("distrorelay_linux.distro.read", lambda: fedora)
    monkeypatch.setattr("distrorelay_linux.cli.SubprocessRunner", lambda: runner)
    monkeypatch.setattr("distrorelay_linux.cli.Path.home", lambda: home)
    monkeypatch.setattr("distrorelay_linux.cli.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("distrorelay_linux.kit.default_search_roots", lambda user=None: [])

    def make(when):
        return linux_backup.create_backup(kit, runner, fedora, home=home, curate=lambda c: [], now=when,
                                          include_sensitive=False)

    return linux_cli, kit, make


def test_c3_setup_names_the_broken_latest_backup_instead_of_saying_there_is_none(tmp_path, monkeypatch, capsys):
    linux_cli, kit, make = _setup_cli(tmp_path, monkeypatch)
    only = make(datetime(2026, 10, 25, 9, 7, 13))
    (only.path / linux_backup.DOTFILES).write_bytes(b"rusak")  # seperti K-6

    assert linux_cli.main(["--kit", str(kit.root), "setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "belum ada backup" not in out
    assert f"backup terbaru {only.path.name}" in out and "RUSAK" in out and "bootstrap.sh backups" in out


def test_c3_setup_says_when_an_older_backup_is_used(tmp_path, monkeypatch, capsys):
    linux_cli, kit, make = _setup_cli(tmp_path, monkeypatch)
    older = make(datetime(2026, 10, 24, 9, 0, 0))
    newer = make(datetime(2026, 10, 25, 9, 0, 0))
    (newer.path / linux_backup.DOTFILES).write_bytes(b"rusak")

    assert linux_cli.main(["--kit", str(kit.root), "setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"Pulihkan dotfiles dari {older.path}" in out
    assert f"Backup terbaru {newer.path.name}" in out and f"dipakai backup {older.path.name}" in out


# --- C-4, C-5: iso download menyebut unduhan yang dilanjutkan dan ukuran ISO yang ditolak -------


@needs_gpgv
def test_c4_resumed_download_is_announced_before_it_starts(tmp_path):
    from distrorelay.windows.iso import download as download_mod
    from distrorelay.windows.iso import manager

    tools = make_iso_tools(tmp_path)
    release = fake_release(DETACHED_CHECKSUM)
    folder = iso_mod.release_folder(release, tools.root)
    folder.mkdir(parents=True)
    download_mod.part_path(folder / release.iso_name).write_bytes(b"x" * 2048)
    said = []
    manager.prepare(release, TEST_KEYS, tools, say=lambda message: said.append((message, len(tools.download.calls))))
    assert said == [("Melanjutkan unduhan dari 2.0 KB (fake-1.0-x86_64.iso.part)", 0)]  # sebelum unduhan dipanggil


@needs_gpgv
def test_c4_fresh_download_says_nothing(tmp_path):
    from distrorelay.windows.iso import manager

    said = []
    manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, make_iso_tools(tmp_path), say=said.append)
    assert said == []


@needs_gpgv
def test_c5_rejected_iso_note_gives_path_and_size(tmp_path):
    from distrorelay.windows.iso import manager

    tampered = bytearray(FAKE_ISO)
    tampered[1000] ^= 1
    result = manager.prepare(fake_release(DETACHED_CHECKSUM), TEST_KEYS, make_iso_tools(tmp_path, content=bytes(tampered)))
    assert not result.ok
    rejected = result.folder / "fake-1.0-x86_64.iso.rejected"
    note = next(n for n in result.notes if ".rejected" in n)
    assert str(rejected) in note and "boleh dihapus" in note
    assert manager.rejected_note(result.folder / "fake-1.0-x86_64.iso") == note


# --- C-6, C-7: BOM dari pipe PowerShell ----------------------------------------------------------


def test_c6_bom_from_powershell_pipe_is_dropped_from_ordinary_answers(monkeypatch):
    from distrorelay.cli import prompts

    monkeypatch.setattr("builtins.input", lambda prompt="": "﻿ya")
    assert prompts.ask("Ketik ya: ") == "ya"


def test_c6_confirmation_words_stay_strict(monkeypatch):
    from distrorelay.cli import prompts
    from distrorelay.windows.workflow import DELETE_WORD, typed

    monkeypatch.setattr("builtins.input", lambda prompt="": "﻿HAPUS")
    assert not typed(prompts.ask_word("Ketik HAPUS: "), DELETE_WORD)


@pytest.mark.skipif(sys.platform != "win32", reason="perilaku stdin Windows (di Linux \\r memang tidak dibuang)")
def test_c7_piped_line_reaches_python_without_carriage_return():
    """Di Windows `input()` membaca stdin dalam mode teks: \\r\\n dari PowerShell sudah jadi \\n, tidak ada \\r tersisa."""
    import subprocess

    out = subprocess.run([sys.executable, "-c", "print(repr(input()))"], input=b"HAPUS\r\n", capture_output=True)
    assert out.stdout.strip() == b"'HAPUS'"


# --- T-7: paket inti sistem Debian tidak ikut checklist backup ----------------------------------

KALI = Path(__file__).parent / "fixtures" / "kali-wsl"  # apt-mark + dpkg-query dari Kali WSL di laptop, 25 Okt 2026


def _debian_runner(dpkg=None, dpkg_code=0):
    runner = LinuxFakeRunner()
    runner.add("apt-mark showmanual", 0, (KALI / "apt-mark-showmanual.txt").read_text(encoding="utf-8"))
    runner.add("dpkg-query", dpkg_code, (KALI / "dpkg-query.txt").read_text(encoding="utf-8") if dpkg is None else dpkg)
    return runner


def test_t7_kali_wsl_core_packages_are_not_candidates():
    found = linux_backup.installed_packages(_debian_runner(), detect(OS_RELEASE["ubuntu"]))
    kept, _ = linux_backup.filter_packages(found.native, linux_backup.base_patterns("debian"), [])
    # Sebelum perbaikan: 134 kandidat. Sisanya paket Priority optional yang memang perlu dikurasi pengguna.
    assert kept == ["fastfetch", "logsave", "openssl-provider-legacy", "readline-common", "sqv", "whiptail", "zstd"]
    assert {"libc6", "libc-bin", "init", "udev", "login", "passwd", "perl-base", "libsystemd0", "mount"} <= set(found.core)
    assert found.problems == []


def test_t7_metadata_rules_essential_priority_and_lib_sections():
    dpkg = "\n".join([
        "coreutils\tyes\trequired\tutils",   # Essential
        "nano\tno\timportant\teditors",      # Priority important
        "libfoo1\tno\toptional\tuniverse/libs",  # pustaka (Ubuntu: komponen/section)
        "libold0\tno\toptional\toldlibs",
        "vlc\tno\toptional\tvideo",
        "baris-rusak",
    ]) + "\n"
    runner = LinuxFakeRunner()
    runner.add("apt-mark showmanual", 0, "coreutils\nnano\nlibfoo1\nlibold0:i386\nvlc\n")
    runner.add("dpkg-query", 0, dpkg)
    found = linux_backup.installed_packages(runner, detect(OS_RELEASE["ubuntu"]))
    assert found.native == ["vlc"]
    assert found.core == ["coreutils", "nano", "libfoo1", "libold0:i386"]  # nama:arch dari apt-mark tetap dikenali


def test_t7_failed_dpkg_query_keeps_old_behaviour_and_says_so():
    found = linux_backup.installed_packages(_debian_runner(dpkg="", dpkg_code=2), detect(OS_RELEASE["ubuntu"]))
    assert len(found.native) == 148 and found.core == []
    assert len(found.problems) == 1 and "dpkg-query" in found.problems[0]


def test_t7_backup_notes_how_many_core_packages_were_left_out(tmp_path):
    kit = make_kit(tmp_path / "usb")
    seen = []
    result = linux_backup.create_backup(
        kit, _debian_runner(), detect(OS_RELEASE["ubuntu"]), home=make_home(tmp_path / "home"),
        curate=lambda candidates: seen.extend(candidates) or [], now=datetime(2026, 10, 25, 9, 7, 13),
        include_sensitive=False,
    )
    assert "libc6" not in seen and "fastfetch" in seen
    assert any("140 paket inti sistem / pustaka tidak dibawa" in note for note in result.notes)
