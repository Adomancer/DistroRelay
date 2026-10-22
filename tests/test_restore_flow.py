"""RestoreFlow (M2c fase B): menu Pulihkan lewat alur, dengan tiket PULIHKAN sekali pakai. Laptop simulasi."""

import argparse
import dataclasses
import json

import pytest

from distrorelay.windows.workflow import FlowError, RestoreFlow
from test_execute import ARCH, FEDORA
from test_workflow import make_flow, ready_for_gate


def uninstalled(tmp_path):
    """Laptop simulasi sesudah uninstall Fedora + Arch + grub\\ (dengan backup lokal + eksternal)."""
    flow, bcd, ext = make_flow(tmp_path)
    backup = ready_for_gate(flow, ext)
    assert flow.gate().ok
    assert flow.execute(flow.authorize("HAPUS")).completed
    assert FEDORA not in bcd.entries and ARCH not in bcd.entries
    return flow, bcd, backup.backup_id


def fedora_entries(bcd):
    return [e for e in bcd.entries.values() if e["description"] == "Fedora"]


def test_restore_esp_folder_and_entry_after_uninstall(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    esp_root = tmp_path / "esp"
    assert not (esp_root / "EFI" / "fedora").exists()

    restore = RestoreFlow(flow.deps)
    [info] = restore.backups()
    assert info.backup_id == backup_id and info.usb_copy
    assert all(v.ok for v in restore.load(backup_id))

    esp = {c.key: c for c in restore.esp_choices()}
    assert {"EFI\\fedora", "EFI\\Arch", "grub", "EFI\\HP", "mach_kernel"} <= set(esp)
    assert esp["EFI\\Microsoft"].blocked and esp["EFI\\fedora"].blocked is None
    entries = {c.label.split("'")[1]: c for c in restore.entry_choices()}
    assert entries["Windows Boot Manager"].blocked and entries["Fedora"].blocked is None

    assert restore.select(["EFI\\fedora"], [entries["Fedora"].key]) is None
    assert "EFI\\fedora" in restore.summary()
    assert restore.authorize("pulihkan") is None
    ticket = restore.authorize("PULIHKAN ")
    results = restore.run(ticket)
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert (esp_root / "EFI" / "fedora" / "shimx64.efi").exists()
    [entry] = fedora_entries(bcd)
    assert entry["path"].lower() == "\\efi\\fedora\\shimx64.efi"

    with pytest.raises(FlowError):
        restore.run(ticket)  # sekali pakai


def test_restore_ticket_is_void_after_selection_changes(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    restore = RestoreFlow(flow.deps)
    restore.load(backup_id)
    assert restore.select(["EFI\\fedora"], []) is None
    ticket = restore.authorize("PULIHKAN")
    assert restore.select(["EFI\\Arch"], []) is None
    with pytest.raises(FlowError, match="Tiket pemulihan tidak berlaku"):
        restore.run(ticket)
    with pytest.raises(FlowError):
        restore.run(None)
    assert not (tmp_path / "esp" / "EFI" / "fedora").exists()


def test_restore_refuses_windows_items_and_bad_backups(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    restore = RestoreFlow(flow.deps)
    for bad in ("tidak-ada", "..\\local", ""):
        assert not all(v.ok for v in restore.load(bad))
        with pytest.raises(FlowError):
            restore.esp_choices()

    restore.load(backup_id)
    assert "dilindungi" in restore.select(["EFI\\Microsoft\\Boot"], [])
    assert "bcdboot" in restore.select([], ["Windows Boot Manager"])
    assert restore.select([], []) == "Belum ada yang dipilih"
    with pytest.raises(FlowError):
        restore.authorize("PULIHKAN")  # tidak ada pilihan yang berlaku


def test_restore_reverifies_backup_before_running(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    restore = RestoreFlow(flow.deps)
    restore.load(backup_id)
    assert restore.select(["EFI\\fedora"], []) is None
    ticket = restore.authorize("PULIHKAN")
    (flow.deps.backups_root / backup_id / "nvram-raw.json").write_text("{}", encoding="utf-8")  # rusak sesudah dipilih
    results = restore.run(ticket)
    assert results and not any(r.ok for r in results)
    assert not (tmp_path / "esp" / "EFI" / "fedora").exists()


def test_restore_refuses_a_changed_manifest(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    manifest_path = flow.deps.backups_root / backup_id / "manifest.json"
    restore = RestoreFlow(flow.deps)
    restore.load(backup_id)
    assert restore.select(["EFI\\fedora"], []) is None
    ticket = restore.authorize("PULIHKAN")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "diubah"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    [result] = restore.run(ticket)
    assert not result.ok and "berubah" in result.message
    assert not (tmp_path / "esp" / "EFI" / "fedora").exists()


def test_restore_refuses_a_different_esp(tmp_path):
    from helpers import laptop_inventory

    flow, bcd, backup_id = uninstalled(tmp_path)
    other = laptop_inventory()
    other.esp().guid = "00000000-0000-4000-8000-000000000000"  # mis. SSD lain / ESP dibuat ulang
    restore = RestoreFlow(dataclasses.replace(flow.deps, load_inventory=lambda runner: other))
    restore.load(backup_id)
    assert restore.select(["EFI\\fedora"], []) is None
    [result] = restore.run(restore.authorize("PULIHKAN"))
    assert not result.ok and "GUID ESP" in result.message
    assert not (tmp_path / "esp" / "EFI" / "fedora").exists()


def test_cli_restore_runs_on_the_flow(tmp_path, monkeypatch):
    from distrorelay.__main__ import _run_restore_command

    flow, bcd, backup_id = uninstalled(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "PULIHKAN")
    args = argparse.Namespace(backup=backup_id, esp_item=["EFI\\fedora"], boot_entry=["Fedora"])
    assert _run_restore_command(args, flow=RestoreFlow(flow.deps)) == 0
    assert (tmp_path / "esp" / "EFI" / "fedora").is_dir() and len(fedora_entries(bcd)) == 1

    monkeypatch.setattr("builtins.input", lambda prompt="": "pulihkan")
    args = argparse.Namespace(backup=backup_id, esp_item=["EFI\\Arch"], boot_entry=[])
    assert _run_restore_command(args, flow=RestoreFlow(flow.deps)) == 1
    assert not (tmp_path / "esp" / "EFI" / "Arch").exists()
