import json

from distrohop.windows import gpt_types
from distrohop.windows.disks import parse_inventory, redact_serials
from helpers import P5_GUID, fixture_text, laptop_inventory


def test_gpt_lookup():
    assert gpt_types.lookup("{BC13C2FF-59E6-4262-A352-B275FD6F7172}").category == "linux"
    assert gpt_types.lookup("{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}").category == "esp"
    assert gpt_types.lookup("{12345678-0000-0000-0000-000000000000}").category == "unknown"
    assert gpt_types.lookup(None).category == "unknown"
    assert gpt_types.normalize_guid(" {ABC} ") == "abc"


def test_laptop_fixture_layout():
    inventory = laptop_inventory()
    assert len(inventory.disks) == 1
    disk = inventory.disks[0]
    assert disk.partition_style == "GPT" and disk.sector_size == 512
    assert [p.id for p in inventory.partitions] == [f"disk0-p{n}" for n in range(1, 6)]

    esp, msr, windows, recovery, linux = inventory.partitions
    assert esp.category == "esp" and esp.is_protected and inventory.esp() is esp
    assert msr.access_paths == []  # [null] dari PowerShell dibuang
    assert windows.drive_letter == "C" and windows.mount_points == ["C:\\"] and windows.is_protected
    assert recovery.category == "recovery" and recovery.is_protected
    assert linux.guid == P5_GUID
    assert linux.gpt_type == gpt_types.XBOOTLDR and linux.is_linux and not linux.is_protected
    assert linux.drive_letter is None and linux.mount_points == []


def test_partitions_are_ordered_by_offset_and_disk_is_full():
    inventory = laptop_inventory()
    offsets = [p.offset for p in inventory.partitions_on(0)]
    assert offsets == sorted(offsets)
    # Partisi #5 ada setelah Recovery, jadi ruang bekasnya tidak bersebelahan dengan C:.
    windows, recovery, linux = inventory.partitions[2:]
    assert windows.end == recovery.offset and recovery.end < linux.offset
    assert inventory.free_regions() == []


def test_fingerprint_changes_when_layout_changes():
    first, second = laptop_inventory(), laptop_inventory()
    assert first.fingerprint() == second.fingerprint()
    second.partitions[-1].size -= 4096
    assert first.fingerprint() != second.fingerprint()


def test_single_object_and_value_count_shapes():
    text = json.dumps(
        {
            "Disks": {"Number": 0, "Size": 1, "PartitionStyle": "GPT"},
            "Partitions": {"value": [{"DiskNumber": 0, "PartitionNumber": 1, "DriveLetter": "\u0000"}], "Count": 1},
        }
    )
    inventory = parse_inventory(text)
    assert len(inventory.disks) == 1 and len(inventory.partitions) == 1
    assert inventory.partitions[0].drive_letter is None


def test_serials_are_redacted():
    text = '{"SerialNumber":"ABC 123","Size":1}'
    assert redact_serials(text) == '{"SerialNumber":"<disamarkan>","Size":1}'
    assert "<disamarkan>" in fixture_text("disks_laptop.json")
