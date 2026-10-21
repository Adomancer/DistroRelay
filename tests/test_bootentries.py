from distrohop.windows.bootentries import BOOTMGR_ID, merge_entries, parse_firmware
from distrohop.windows.nvram import parse_load_option, snapshot_from_raw
from helpers import ESP_GUID, WINDOWS_BCD, fixture_text, nvram_raw_laptop

FEDORA_BCD = "{0b5f2d1e-7a39-11ef-9a6b-806e6f6e6963}"
PXE_BCD = "{0b5f2d1f-7a39-11ef-9a6b-806e6f6e6963}"


def test_parse_english_output():
    config = parse_firmware(fixture_text("bcdedit_firmware_synthetic_en.txt"))
    assert config.display_order == [FEDORA_BCD, BOOTMGR_ID, PXE_BCD]
    assert config.timeout == "0"
    windows = config.find(BOOTMGR_ID)
    assert windows.is_windows_boot_manager and windows.description == "Windows Boot Manager"
    fedora = config.find(FEDORA_BCD)
    assert fedora.path == "\\EFI\\fedora\\shimx64.efi" and fedora.description == "Fedora"
    pxe = config.find(PXE_BCD)
    assert pxe.path is None and pxe.description.startswith("UEFI: PXE")


def test_parse_localized_output_without_english_field_names():
    text = fixture_text("bcdedit_firmware_synthetic_en.txt")
    for english, german in [
        ("Firmware Boot Manager", "Firmware-Start-Manager"),
        ("identifier", "Bezeichner"),
        ("displayorder", "Anzeigereihenfolge"),
        ("timeout", "Zeitlimit"),
        ("device", "Gerät"),
        ("path", "Pfad"),
        ("description", "Beschreibung"),
        ("locale", "Gebietsschema"),
        ("Windows Boot Manager", "Windows-Start-Manager"),
    ]:
        text = text.replace(english, german)
    config = parse_firmware(text)
    assert config.display_order == [FEDORA_BCD, BOOTMGR_ID, PXE_BCD]
    assert config.find(BOOTMGR_ID).description == "Windows-Start-Manager"
    assert config.find(FEDORA_BCD).path == "\\EFI\\fedora\\shimx64.efi"
    assert config.find(FEDORA_BCD).description == "Fedora"
    assert config.find(PXE_BCD).description.startswith("UEFI: PXE")


def test_parse_tolerates_windows_line_endings():
    text = fixture_text("bcdedit_firmware_laptop.txt")
    expected = parse_firmware(text)
    assert len(expected.entries) == 5
    for newline in ("\r\n", "\r\r\n"):  # \r\r\n = file yang pernah ditulis ulang dalam mode teks
        assert parse_firmware(text.replace("\n", newline)) == expected


def test_nvram_load_option_parsing():
    snapshot = snapshot_from_raw(nvram_raw_laptop())
    assert snapshot.boot_order == [1, 0, 3] and snapshot.boot_current == 0
    fedora = snapshot.options[1]
    assert fedora.description == "Fedora" and fedora.active
    assert fedora.partition_guid == ESP_GUID
    assert fedora.file_path == "\\EFI\\fedora\\shimx64.efi"
    assert fedora.device_summary.startswith(f"HD(1,GPT,{ESP_GUID})")
    windows = snapshot.options[0]
    assert windows.bcd_object == WINDOWS_BCD
    pxe = snapshot.options[3]
    assert pxe.file_path is None and pxe.partition_guid is None
    assert "MAC" in pxe.device_summary and "IPv4" in pxe.device_summary


def test_short_or_broken_load_option_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        parse_load_option(1, b"\x01\x00")


def test_merge_matches_both_sources_and_uses_nvram_order():
    config = parse_firmware(fixture_text("bcdedit_firmware_synthetic_en.txt"))
    snapshot = snapshot_from_raw(nvram_raw_laptop())
    entries, notes = merge_entries(config, snapshot)
    assert notes == []
    assert [e.description for e in entries] == [
        "Fedora",
        "Windows Boot Manager",
        "UEFI: PXE IPv4 Realtek PCIe GBE Family Controller",
    ]
    fedora, windows, pxe = entries
    assert fedora.order == 0 and fedora.bcd_id == FEDORA_BCD and fedora.sources == ["NVRAM", "bcdedit"]
    assert windows.is_windows_boot_manager and windows.bcd_id == BOOTMGR_ID and windows.order == 1
    assert pxe.bcd_id == PXE_BCD and pxe.path is None


def test_merge_reports_mismatches():
    config = parse_firmware(fixture_text("bcdedit_firmware_synthetic_en.txt"))
    raw = nvram_raw_laptop()
    del raw["Boot0001"]  # Fedora hilang dari NVRAM tapi masih ada di bcdedit
    entries, notes = merge_entries(config, snapshot_from_raw(raw))
    assert any("Fedora" in n and "tidak di NVRAM" in n for n in notes)
    assert next(e for e in entries if e.description == "Fedora").sources == ["bcdedit"]


def test_merge_with_only_bcdedit_uses_display_order():
    config = parse_firmware(fixture_text("bcdedit_firmware_synthetic_en.txt"))
    entries, notes = merge_entries(config, None)
    assert notes == []
    assert [e.order for e in entries] == [0, 1, 2]
