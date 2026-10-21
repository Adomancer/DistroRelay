"""Scan end-to-end dengan FakeRunner + pembaca palsu: tidak ada yang menyentuh sistem asli."""

import asyncio
import json
from contextlib import contextmanager

from rich.console import Console

from distrohop import render
from distrohop.tui.app import DistroHopApp
from distrohop.windows.bootrefs import VERDICT_LEFTOVER
from distrohop.windows.esp import MountedEsp
from distrohop.windows.nvram import snapshot_from_raw
from distrohop.windows.runner import FakeRunner, ReadOnlyRunner
from distrohop.windows.scanner import ScanDeps, report_to_json, run_scan, save_raw
from helpers import BOOT_UUID, ext4_image, fat32_image, fixture_text, laptop_esp_files, nvram_raw_laptop, write_tree

BITLOCKER_JSON = '[{"DriveLetter":"C:","ProtectionStatus":1,"ConversionStatus":1,"EncryptionMethod":7,"VolumeType":0}]'


def make_deps(tmp_path, admin=True):
    fake = FakeRunner()
    fake.add("ps:disks", fixture_text("disks_laptop.json"))
    fake.add("bcdedit /enum firmware /v", fixture_text("bcdedit_firmware_synthetic_en.txt"))
    fake.add("ps:bitlocker", BITLOCKER_JSON)
    write_tree(tmp_path, laptop_esp_files())

    @contextmanager
    def mount(runner, esp):
        yield MountedEsp(tmp_path, "Z", temporary=True)

    def read_head(part, sector_size):
        return {"disk0-p5": ext4_image(BOOT_UUID), "disk0-p1": fat32_image()}.get(part.id, bytes(4096))

    deps = ScanDeps(
        runner=ReadOnlyRunner(fake),
        is_admin=lambda: admin,
        read_nvram=lambda: snapshot_from_raw(nvram_raw_laptop()),
        read_partition_head=read_head,
        mount_esp=mount,
    )
    return deps, fake


def test_full_scan_as_admin(tmp_path):
    deps, fake = make_deps(tmp_path)
    messages = []
    report = run_scan(deps, progress=messages.append)

    assert [i for i in report.issues if i.severity == "error"] == []
    assert report.fs_probe_complete
    assert report.fsinfo["disk0-p5"].last_mounted == "/boot"
    assert report.fsinfo["disk0-p1"].fs_type == "vfat"
    assert report.bitlocker[0].needs_recovery_key
    assert report.esp.temporary_mount and report.esp.mounted_at == "Z:"
    assert [e.description for e in report.boot_entries][:2] == ["Fedora", "Windows Boot Manager"]
    assert all(e.sources == ["NVRAM", "bcdedit"] for e in report.boot_entries)
    fedora = next(i for i in report.analysis.installations if i.key == "efi:fedora")
    assert fedora.verdict == VERDICT_LEFTOVER
    assert messages[-1] == "Scan selesai."
    assert "disks.json" in report.raw and "<disamarkan>" in report.raw["disks.json"]

    report.inventory.disks[0].serial = "SERIAL-ASLI-123"
    data = json.loads(report_to_json(report))
    assert data["analysis"]["installations"][0]["verdict"] == VERDICT_LEFTOVER
    assert data["inventory"]["disks"][0]["serial"] == "<disamarkan>"
    assert "SERIAL-ASLI-123" not in report_to_json(report)


def test_save_raw_keeps_single_line_endings(tmp_path):
    deps, fake = make_deps(tmp_path / "esp")
    fake.add("bcdedit /enum firmware /v", fixture_text("bcdedit_firmware_laptop.txt").replace("\n", "\r\n"))
    report = run_scan(deps)
    for path in save_raw(report, tmp_path / "raw"):
        assert b"\r" not in path.read_bytes(), path.name


def test_scan_without_admin_skips_privileged_steps(tmp_path):
    deps, fake = make_deps(tmp_path, admin=False)
    report = run_scan(deps)
    assert {i.step for i in report.issues} == {"fsprobe", "bcdedit", "nvram", "esp", "bitlocker"}
    assert all(i.severity == "info" for i in report.issues)
    assert [c[0] for c in fake.calls] == ["powershell.exe"]  # hanya inventaris disk
    unattached = next(i for i in report.analysis.installations if i.key == "unattached")
    assert [c.ref for c in unattached.components] == ["disk0-p5"]


def test_failing_step_is_reported_not_raised(tmp_path):
    deps, fake = make_deps(tmp_path)
    fake.add("bcdedit /enum firmware /v", returncode=1, stderr="Access is denied.")

    def broken_nvram():
        raise OSError("firmware tidak merespons")

    deps.read_nvram = broken_nvram
    report = run_scan(deps)
    steps = {i.step: i for i in report.issues}
    assert steps["bcdedit"].severity == "error" and "Access is denied" in steps["bcdedit"].message
    assert steps["nvram"].severity == "error"
    assert report.analysis is not None


def test_render_outputs(tmp_path):
    deps, _ = make_deps(tmp_path)
    report = run_scan(deps)
    console = Console(record=True, width=120)
    console.print(render.to_rich_tree("hasil", render.summary_nodes(report)))
    console.print(render.info_text(report))
    output = console.export_text()
    assert "Fedora — kemungkinan sisa" in output
    assert "ada di urutan pertama" in output
    assert "proteksi AKTIF" in output
    assert len(render.partition_rows(report)) == 5
    assert len(render.entry_rows(report)) == 3
    assert render.esp_nodes(report)[0].children


def test_tui_shows_report(tmp_path):
    deps, _ = make_deps(tmp_path)
    report = run_scan(deps)

    async def scenario():
        app = DistroHopApp(scan=lambda progress=None: report)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.report is report
            summary = app.query_one("#summary")
            labels = [str(node.label) for node in summary.root.children]
            assert any("Fedora" in label and "kemungkinan sisa" in label for label in labels)
            assert app.query_one("#partitions").row_count == 5

    asyncio.run(scenario())
