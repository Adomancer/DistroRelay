import json
from datetime import datetime
from pathlib import Path

import pytest

from distrorelay.windows.backup import BackupError, backup_runner, create_backup, verify_backup_dir
from distrorelay.windows.scanner import run_scan
from helpers import fake_mount, laptop_esp_files, scan_deps

NOW = datetime(2026, 10, 21, 19, 30, 0)


def fake_export(args):
    """Seperti bcdedit /export asli: file hive + log transaksinya."""
    target = Path(args[2])
    target.write_bytes(b"regf hive palsu")
    Path(str(target) + ".LOG").write_bytes(b"log")
    Path(str(target) + ".LOG1").write_bytes(b"")


def fake_store_enum(args):
    """Seperti bcdedit /store asli: me-load hive memperbarui header file yang dibaca."""
    with open(args[2], "ab") as hive:
        hive.write(b"header diperbarui")


def setup(tmp_path, files=None):
    esp_root = tmp_path / "esp"
    deps, fake = scan_deps(esp_root, files=files)
    report = run_scan(deps)
    fake.add("bcdedit /export *", effect=fake_export)
    fake.add(
        "bcdedit /store *",
        stdout="Windows Boot Manager\n--------------------\nidentifier {bootmgr}\n",
        effect=fake_store_enum,
    )
    runner = backup_runner(fake, root=tmp_path / "local")
    return report, runner, esp_root, fake


def backup(tmp_path, report, runner, esp_root, **kwargs):
    return create_backup(
        report, runner, fake_mount(esp_root), local_root=tmp_path / "local", usb_root=tmp_path / "usb", now=NOW, **kwargs
    )


def test_full_backup_is_verified_locally_and_on_usb(tmp_path):
    report, runner, esp_root, fake = setup(tmp_path)
    result = backup(tmp_path, report, runner, esp_root)

    assert result.ok, [v for v in result.verifications if not v.ok]
    assert result.backup_id == "20261021-193000"
    assert [f.name for f in result.files] == [
        "esp.zip",
        "bcd-store.bak",
        "bcd-store.bak.LOG",
        "bcd-store.bak.LOG1",
        "firmware-entries.txt",
        "firmware-entries.json",
        "nvram-raw.json",
        "partitions.json",
        "fsinfo.json",
    ]
    # Pengecekan BCD dijalankan pada salinan sementara, jadi file backup tidak ikut berubah.
    store_call = next(c for c in fake.calls if c[:2] == ("bcdedit", "/store"))
    assert not Path(store_call[2]).is_relative_to(tmp_path / "local")
    assert (tmp_path / "local" / result.backup_id / "bcd-store.bak").read_bytes() == b"regf hive palsu"
    assert (Path(result.usb_dir) / "bcd-store.bak.LOG").exists()
    assert len(result.esp_files) == len(laptop_esp_files())
    usb = Path(result.usb_dir)
    assert usb == tmp_path / "usb" / "DistroRelay" / "backups" / "20261021-193000"
    assert (usb / "esp" / "EFI" / "fedora" / "shimx64.efi").read_bytes() == (esp_root / "EFI" / "fedora" / "shimx64.efi").read_bytes()
    assert {v.name for v in result.verifications} >= {
        "Isi esp.zip",
        "BCD store bisa dibaca",
        "Hash salinan USB",
        "Folder esp\\ di USB (untuk xcopy dari WinRE)",
    }
    manifest = json.loads((usb / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["layout_fingerprint"] == report.inventory.fingerprint()
    # NVRAM di backup TIDAK disamarkan: ini backup milik sendiri, harus bisa dipakai untuk pemulihan.
    assert json.loads((usb / "nvram-raw.json").read_text(encoding="utf-8")) == report.nvram.raw
    assert all(v["ok"] for v in json.loads((tmp_path / "local" / result.backup_id / "verification.json").read_text("utf-8")))


def test_local_only_backup_is_verified_but_marked_not_enough_for_execution(tmp_path):
    report, runner, esp_root, fake = setup(tmp_path)
    result = create_backup(report, runner, fake_mount(esp_root), local_root=tmp_path / "local", local_only=True, now=NOW)
    assert result.ok and result.local_only and result.usb_dir is None
    assert "Salinan USB" not in {v.name for v in result.verifications}
    manifest = json.loads((tmp_path / "local" / result.backup_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["usb_copy"] is False
    assert not (tmp_path / "usb").exists()


def test_missing_usb_without_local_only_fails(tmp_path):
    report, runner, esp_root, fake = setup(tmp_path)
    result = create_backup(report, runner, fake_mount(esp_root), local_root=tmp_path / "local", now=NOW)
    assert not result.ok
    assert [v.name for v in result.verifications if not v.ok] == ["Salinan USB"]
    with pytest.raises(BackupError, match="Pilih salah satu"):
        create_backup(report, runner, fake_mount(esp_root), local_root=tmp_path / "x", usb_root=tmp_path / "usb", local_only=True)


def test_external_drive_write_error_is_reported_not_raised(tmp_path, monkeypatch):
    """Kasus nyata 2026-10-21: drive eksternal menolak tulis (WinError 1117) setelah ±7 MB."""
    import shutil

    report, runner, esp_root, fake = setup(tmp_path)
    real_copy = shutil.copy2

    def failing_copy(src, dst, *args, **kwargs):
        if str(dst).startswith(str(tmp_path / "usb")) and Path(src).name == "esp.zip":
            raise OSError(22, "Invalid argument (I/O device error)")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr("distrorelay.windows.backup.shutil.copy2", failing_copy)
    result = backup(tmp_path, report, runner, esp_root)

    assert not result.ok and result.usb_dir is None
    failed = [v for v in result.verifications if not v.ok]
    assert [v.name for v in failed] == ["Salinan USB"] and "backup lokal tetap utuh" in failed[0].message
    assert not (tmp_path / "usb" / "DistroRelay" / "backups" / result.backup_id).exists()  # sisa setengah jadi dihapus
    local = tmp_path / "local" / result.backup_id
    assert all(v.ok for v in verify_backup_dir(local))
    assert (local / "verification.json").exists()


def test_locked_file_is_skipped_and_recorded(tmp_path):
    files = dict(laptop_esp_files(), **{"EFI\\Microsoft\\Boot\\BCD": b"hive"})
    report, runner, esp_root, fake = setup(tmp_path, files)

    def opener(path, mode):
        if path.name == "BCD":
            raise PermissionError("dikunci Windows")
        return open(path, mode)

    result = backup(tmp_path, report, runner, esp_root, opener=opener)
    assert result.skipped == ["EFI\\Microsoft\\Boot\\BCD"]
    assert result.ok
    manifest = json.loads((Path(result.usb_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["skipped"][0]["path"] == "EFI\\Microsoft\\Boot\\BCD"


def test_tampered_usb_copy_is_detected(tmp_path):
    report, runner, esp_root, fake = setup(tmp_path)
    result = backup(tmp_path, report, runner, esp_root)
    usb = Path(result.usb_dir)
    assert all(v.ok for v in verify_backup_dir(usb))

    (usb / "esp" / "EFI" / "fedora" / "grub.cfg").write_text("diubah", encoding="utf-8")
    (usb / "nvram-raw.json").write_text("{}", encoding="utf-8")
    failed = {v.name for v in verify_backup_dir(usb) if not v.ok}
    assert failed == {"Hash file backup", "Folder esp\\"}
    assert all(v.ok for v in verify_backup_dir(tmp_path / "local" / result.backup_id))


def test_esp_changed_since_scan_fails_verification(tmp_path):
    report, runner, esp_root, fake = setup(tmp_path)
    (esp_root / "EFI" / "fedora" / "grub.cfg").write_text("berubah setelah scan", encoding="utf-8")
    result = backup(tmp_path, report, runner, esp_root)
    failed = [v for v in result.verifications if not v.ok]
    assert [v.name for v in failed] == ["ESP sama dengan saat scan"]
    assert not result.ok


def test_failed_export_aborts(tmp_path):
    report, runner, esp_root, fake = setup(tmp_path)
    fake.add("bcdedit /export *", returncode=1, stderr="Access is denied.")
    with pytest.raises(BackupError, match="Access is denied"):
        backup(tmp_path, report, runner, esp_root)


def test_incomplete_scan_is_refused(tmp_path):
    deps, fake = scan_deps(tmp_path / "esp", admin=False)
    report = run_scan(deps)
    with pytest.raises(BackupError, match="Scan belum lengkap"):
        create_backup(report, backup_runner(fake, root=tmp_path), fake_mount(tmp_path / "esp"), local_root=tmp_path)
