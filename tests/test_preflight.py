import copy
import json

from distrohop.windows import disks
from distrohop.windows.backup import backup_runner
from distrohop.windows.preflight import FAIL, MANUAL, OK, run_preflight
from distrohop.windows.scanner import run_scan
from helpers import BITLOCKER_OFF, BITLOCKER_ON, fake_mount, scan_deps, write_tree

VALID = json.dumps(
    {
        "Status": "Valid",
        "Message": "Signature verified.",
        "Subject": "CN=Microsoft Windows, O=Microsoft Corporation, L=Redmond, S=Washington, C=US",
        "Issuer": "CN=Microsoft Windows Production PCA 2011, O=Microsoft Corporation",
        "Thumbprint": "ABC",
    }
)


def with_usb(inventory: disks.DiskInventory, letter: str = "E") -> disks.DiskInventory:
    inventory = copy.deepcopy(inventory)
    inventory.disks.append(disks.Disk(1, "usb-disk", "SanDisk Ultra", "", 32 * 2**30, "GPT", 512, "USB", False, False))
    inventory.partitions.append(
        disks.Partition(1, 1, "usb-part", disks.gpt_types.BASIC_DATA, "Basic data", "basic", 2**20, 30 * 2**30,
                        letter, [f"{letter}:\\"], False, False, False, "Basic", "exFAT", "Ventoy")
    )
    return inventory


def setup(tmp_path, *, usb=True, bitlocker=BITLOCKER_OFF, signature=VALID):
    deps, fake = scan_deps(tmp_path / "esp", bitlocker=bitlocker)
    report = run_scan(deps)
    if usb:
        report.inventory = with_usb(report.inventory)
    fake.add("ps:authenticode", signature)
    fake.add("ps:iso-mount", '"G"')
    fake.add("ps:iso-dismount")
    roots = {"E": tmp_path / "E", "G": tmp_path / "G"}
    for root in roots.values():
        root.mkdir(exist_ok=True)

    def preflight(**overrides):
        kwargs = dict(
            fresh_inventory=copy.deepcopy(report.inventory),
            mount_esp=fake_mount(tmp_path / "esp"),
            root_of=lambda letter: roots[letter],
            disk_usage=lambda path: (0, 0, 10 * 2**30),
        )
        kwargs.update(overrides)
        return run_preflight(report, backup_runner(fake, root=tmp_path / "backups"), **kwargs)

    return report, fake, roots, preflight


def statuses(result):
    return {c.key: c.status for c in result.checks}


def test_ventoy_iso_is_detected_by_mounting_it_read_only(tmp_path):
    report, fake, roots, preflight = setup(tmp_path)
    write_tree(roots["E"], {"Win11_24H2_English_x64.iso": b"iso", "Fedora-Workstation-Live-42.iso": b"iso"})
    write_tree(roots["G"], {"sources\\install.wim": b"wim"})
    result = preflight()

    assert statuses(result) == {
        "admin": OK,
        "layout": OK,
        "windows-boot-manager": OK,
        "bitlocker": OK,
        "emergency-usb": MANUAL,
        "backup-usb": OK,
    }
    assert [(m.drive, m.kind) for m in result.media] == [("E", "iso")]
    assert result.media[0].source.endswith("Win11_24H2_English_x64.iso")
    from distrohop.windows.runner import command_key

    names = [command_key(c) for c in fake.calls]
    assert names.count("ps:iso-mount") == 1 and names.count("ps:iso-dismount") == 1  # ISO Fedora tidak di-mount
    assert result.can_backup and [t.drive for t in result.usb_targets] == ["E"]
    assert any("akan dijadikan pertama" in d for d in result.get("windows-boot-manager").details)


def test_plain_installer_usb_needs_no_mount(tmp_path):
    report, fake, roots, preflight = setup(tmp_path)
    write_tree(roots["E"], {"sources\\install.esd": b"esd"})
    result = preflight()
    assert [(m.drive, m.kind, m.source) for m in result.media] == [("E", "installer", "E:\\sources\\install.esd")]
    from distrohop.windows.runner import command_key

    assert "ps:iso-mount" not in [command_key(c) for c in fake.calls]


def test_no_usb_blocks_backup(tmp_path):
    report, fake, roots, preflight = setup(tmp_path, usb=False)
    result = preflight()
    assert result.get("backup-usb").status == FAIL
    assert "Tidak terdeteksi" in result.get("emergency-usb").message
    assert not result.can_backup
    assert result.can_backup_locally  # mode coba --local-only tetap bisa jalan


def test_invalid_signature_and_changed_layout_fail(tmp_path):
    unsigned = json.dumps({"Status": "NotSigned", "Message": "Tidak ditandatangani", "Subject": None})
    report, fake, roots, preflight = setup(tmp_path, signature=unsigned)
    changed = copy.deepcopy(report.inventory)
    changed.partitions[-1].size -= 4096
    result = preflight(fresh_inventory=changed)
    assert result.get("windows-boot-manager").status == FAIL
    assert "NotSigned" in result.get("windows-boot-manager").message
    assert result.get("layout").status == FAIL
    assert not result.can_backup
    assert {c.key for c in result.blocking} == {"windows-boot-manager", "layout"}


def test_external_hdd_on_any_bus_is_a_backup_target_but_system_disk_never_is(tmp_path):
    report, fake, roots, preflight = setup(tmp_path, usb=False)
    inventory = report.inventory
    inventory.disks.append(disks.Disk(1, "hdd", "WD Elements", "", 2 * 2**40, "GPT", 512, "SCSI", False, False))
    inventory.partitions.append(
        disks.Partition(1, 1, "hdd-part", disks.gpt_types.BASIC_DATA, "Basic data", "basic", 2**20, 2**40,
                        "E", ["E:\\"], False, False, False, "Basic", "NTFS", "Data")
    )
    roots["C"] = tmp_path / "C"
    roots["C"].mkdir()
    result = preflight(fresh_inventory=copy.deepcopy(inventory))
    assert [t.drive for t in result.usb_targets] == ["E"]  # C: ada di SSD Windows, tidak dihitung
    assert "WD Elements (SCSI)" in result.get("backup-usb").details[0]
    assert result.can_backup


def test_bitlocker_on_needs_confirmation(tmp_path):
    report, fake, roots, preflight = setup(tmp_path, bitlocker=BITLOCKER_ON)
    result = preflight()
    assert result.get("bitlocker").status == MANUAL
    assert "recovery key" in result.get("bitlocker").message
