"""Skenario reference graph di ESP palsu (folder temp). Tidak menyentuh firmware atau disk asli."""

from distrohop.windows import gpt_types
from distrohop.windows.bootentries import merge_entries
from distrohop.windows.bootrefs import (
    VERDICT_IN_USE,
    VERDICT_LEFTOVER,
    VERDICT_NOT_TARGET,
    VERDICT_PROTECTED,
    VERDICT_UNSURE,
    analyze,
    collect_esp_refs,
    parse_boot_csv,
    refs_from_grub_cfg,
)
from distrohop.windows.fsprobe import FsInfo
from distrohop.windows.nvram import snapshot_from_raw
from helpers import (
    BOOT_UUID,
    BOOTMGFW,
    ESP_GUID,
    FEDORA_CSV,
    add_partition,
    fake_esp,
    file_node,
    hd_node,
    laptop_esp_files,
    laptop_inventory,
    load_option,
    nvram_raw_laptop,
    pe_with_sections,
)

ROOT_UUID = "11111111-2222-4333-8444-555555555555"
ARCH_ROOT_GUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def run(tmp_path, files, inventory=None, fsinfo=None, complete=True, nvram=None):
    inventory = inventory or laptop_inventory()
    esp = fake_esp(tmp_path, inventory, files)
    refs = collect_esp_refs(tmp_path, esp)
    entries, notes = merge_entries(None, snapshot_from_raw(nvram or nvram_raw_laptop()))
    return analyze(inventory, fsinfo or {}, complete, entries, esp, refs, notes)


def by_key(analysis, key):
    return next(i for i in analysis.installations if i.key == key)


def test_grub_stub_and_csv_parsing():
    refs = refs_from_grub_cfg("search.fs_uuid 7d1e0000-0000-4000-8000-000000000001 root\nset prefix=($root)'/boot/grub'", "x")
    assert [r.value for r in refs] == ["7d1e0000-0000-4000-8000-000000000001"]
    fat = refs_from_grub_cfg("search --no-floppy --fs-uuid --set=root 1A2B-3C4D", "x")
    assert [r.value for r in fat] == ["1a2b-3c4d"]
    assert parse_boot_csv(FEDORA_CSV) == [("shimx64.efi", "Fedora")]


def test_laptop_like_fedora_leftover(tmp_path):
    """Kondisi dugaan laptop: root Fedora sudah di-wipe, /boot (#5) + folder ESP + entri tersisa."""
    fsinfo = {"disk0-p5": FsInfo("ext4", uuid=BOOT_UUID, last_mounted="/boot")}
    analysis = run(tmp_path, laptop_esp_files(), fsinfo=fsinfo)

    fedora = by_key(analysis, "efi:fedora")
    assert fedora.name == "Fedora" and fedora.kind == "linux"
    assert fedora.verdict == VERDICT_LEFTOVER
    kinds = [(c.kind, c.ref) for c in fedora.components]
    assert ("boot-entry", "nv:0001") in kinds
    assert ("esp-folder", "EFI\\fedora") in kinds
    assert ("partition", "disk0-p5") in kinds
    assert any("urutan boot PERTAMA" in r for r in fedora.reasons)
    folder = next(c for c in fedora.components if c.kind == "esp-folder")
    assert any("shim fallback akan membuat ulang entri 'Fedora'" in e for e in folder.evidence)

    windows = by_key(analysis, "windows")
    assert windows.verdict == VERDICT_PROTECTED
    assert any("urutan boot ke-2" in r for r in windows.reasons)
    assert all(c.protected for c in windows.components)
    assert {c.ref for c in windows.components if c.kind == "partition"} == {f"disk0-p{n}" for n in range(1, 5)}

    assert by_key(analysis, "firmware").verdict == VERDICT_NOT_TARGET
    assert analysis.boot_order[0].description == "Fedora"
    assert analysis.fallback.owner == "shim (identik dengan EFI\\fedora\\shimx64.efi)"
    assert analysis.fallback.identical_to == ["EFI\\fedora\\shimx64.efi"] and not analysis.fallback.same_as_windows
    assert any("fallback firmware EFI\\Boot\\bootx64.efi identik" in r for r in fedora.reasons)
    assert not any(i.key == "unattached" for i in analysis.installations)


def test_boot_partition_plus_unrelated_root_is_unsure(tmp_path):
    inventory = laptop_inventory()
    add_partition(inventory, 6, gpt_types.LINUX_FILESYSTEM, "99999999-0000-4000-8000-000000000006")
    fsinfo = {
        "disk0-p5": FsInfo("ext4", uuid=BOOT_UUID, last_mounted="/boot"),
        "disk0-p6": FsInfo("btrfs", uuid=ROOT_UUID, label="fedora"),
    }
    fedora = by_key(run(tmp_path, laptop_esp_files(), inventory, fsinfo), "efi:fedora")
    assert fedora.verdict == VERDICT_UNSURE
    assert any("disk0-p6" in r and "mungkin root" in r for r in fedora.reasons)
    assert any("Label filesystem disk0-p6 ('fedora')" in r for r in fedora.reasons)


def test_stub_pointing_to_root_means_still_installed(tmp_path):
    inventory = laptop_inventory()
    add_partition(inventory, 6, gpt_types.LINUX_FILESYSTEM, "99999999-0000-4000-8000-000000000006")
    files = laptop_esp_files()
    files["EFI\\fedora\\grub.cfg"] = f"search --no-floppy --fs-uuid --set=dev {ROOT_UUID}\n"
    fsinfo = {"disk0-p6": FsInfo("ext4", uuid=ROOT_UUID, last_mounted="/")}
    fedora = by_key(run(tmp_path, files, inventory, fsinfo), "efi:fedora")
    assert fedora.verdict == VERDICT_IN_USE


def test_missing_filesystem_means_leftover_but_only_when_probe_complete(tmp_path):
    files = {
        "EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW,
        "EFI\\ubuntu\\shimx64.efi": b"MZ UEFI SHIM",
        "EFI\\ubuntu\\grub.cfg": "search.fs_uuid 7d1e0000-0000-4000-8000-000000000001 root\n",
    }
    readable = {"disk0-p5": FsInfo("ext4", uuid=BOOT_UUID, last_mounted="/boot")}
    ubuntu = by_key(run(tmp_path / "a", files, fsinfo=readable, complete=True), "efi:ubuntu")
    assert ubuntu.verdict == VERDICT_LEFTOVER
    assert any("sudah tidak ada" in r for r in ubuntu.reasons)

    # Belum di-probe sama sekali (bukan admin): jangan simpulkan "sudah tidak ada".
    not_probed = by_key(run(tmp_path / "b", files, fsinfo={}, complete=False), "efi:ubuntu")
    assert not_probed.verdict == VERDICT_UNSURE

    # Sudah di-probe, tapi #5 tidak dikenali atau LUKS: UUID-nya bisa saja tersembunyi di situ.
    unrecognized = by_key(run(tmp_path / "c", files, fsinfo={}, complete=True), "efi:ubuntu")
    assert unrecognized.verdict == VERDICT_UNSURE
    luks = {"disk0-p5": FsInfo("crypto_LUKS", uuid="44444444-0000-4000-8000-000000000000")}
    encrypted = by_key(run(tmp_path / "d", files, fsinfo=luks, complete=True), "efi:ubuntu")
    assert encrypted.verdict == VERDICT_UNSURE
    assert any("disk0-p5" in r for r in encrypted.reasons)


def test_orphan_entry_without_folder(tmp_path):
    raw = nvram_raw_laptop()
    raw["Boot0001"] = load_option("ubuntu", [hd_node(1, ESP_GUID), file_node("\\EFI\\ubuntu\\shimx64.efi")]).hex()
    inventory = laptop_inventory()
    inventory.partitions = [p for p in inventory.partitions if p.category != "linux"]
    analysis = run(tmp_path, {"EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW}, inventory, nvram=raw)
    ubuntu = by_key(analysis, "efi:ubuntu")
    assert ubuntu.name == "Ubuntu" and ubuntu.verdict == VERDICT_LEFTOVER
    entry = ubuntu.components[0]
    assert any("TIDAK ADA" in e for e in entry.evidence)


def test_entry_pointing_to_deleted_partition(tmp_path):
    raw = nvram_raw_laptop()
    raw["Boot0001"] = load_option(
        "Fedora", [hd_node(7, "12345678-1234-4234-8234-123456789abc"), file_node("\\EFI\\fedora\\shimx64.efi")]
    ).hex()
    fedora = by_key(run(tmp_path, laptop_esp_files(), nvram=raw), "efi:fedora")
    entry = next(c for c in fedora.components if c.kind == "boot-entry")
    assert any("tidak ada di disk mana pun" in e for e in entry.evidence)


def test_systemd_boot_arch_is_grouped_with_kernel_files(tmp_path):
    inventory = laptop_inventory()
    add_partition(inventory, 6, gpt_types.LINUX_ROOT_X86_64, ARCH_ROOT_GUID)
    files = {
        "EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW,
        "EFI\\systemd\\systemd-bootx64.efi": b"MZ systemd-boot",
        "loader\\loader.conf": "default arch.conf\ntimeout 3\n",
        "loader\\entries\\arch.conf": (
            "title   Arch Linux\nlinux   /vmlinuz-linux\ninitrd  /intel-ucode.img\ninitrd  /initramfs-linux.img\n"
            f"options root=PARTUUID={ARCH_ROOT_GUID} rw\n"
        ),
        "vmlinuz-linux": b"kernel",
        "initramfs-linux.img": b"initramfs",
        "intel-ucode.img": b"ucode",
        "initramfs-linux-fallback.img": b"fallback",
    }
    raw = nvram_raw_laptop()
    raw["Boot0001"] = load_option(
        "Linux Boot Manager", [hd_node(1, ESP_GUID), file_node("\\EFI\\systemd\\systemd-bootx64.efi")]
    ).hex()
    analysis = run(tmp_path, files, inventory, nvram=raw)
    arch = by_key(analysis, f"sd:{ARCH_ROOT_GUID}")
    assert arch.name == "Arch Linux"
    assert arch.verdict == VERDICT_IN_USE
    refs = {c.ref for c in arch.components}
    assert {"nv:0001", "EFI\\systemd", "loader\\loader.conf", "loader\\entries\\arch.conf"} <= refs
    assert {"vmlinuz-linux", "initramfs-linux.img", "intel-ucode.img", "disk0-p6"} <= refs
    assert not any(i.key == "efi:systemd" for i in analysis.installations)
    # File & partisi yang tidak dirujuk siapa pun tidak diklaim milik Arch.
    unattached = by_key(analysis, "unattached")
    assert [c.ref for c in unattached.components] == ["initramfs-linux-fallback.img", "disk0-p5"]


def test_uki_is_identified_from_osrel(tmp_path):
    inventory = laptop_inventory()
    files = {
        "EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW,
        "EFI\\Linux\\arch-linux.efi": pe_with_sections(
            {".osrel": b'ID=arch\nPRETTY_NAME="Arch Linux"\n', ".cmdline": f"root=UUID={ROOT_UUID} rw".encode()}
        ),
    }
    raw = nvram_raw_laptop()
    raw["Boot0001"] = load_option("Arch UKI", [hd_node(1, ESP_GUID), file_node("\\EFI\\Linux\\arch-linux.efi")]).hex()
    analysis = run(tmp_path, files, inventory, fsinfo={}, complete=True, nvram=raw)
    arch = by_key(analysis, f"sd:{ROOT_UUID}")
    assert arch.name == "Arch Linux"
    assert {c.ref for c in arch.components} >= {"nv:0001", "EFI\\Linux\\arch-linux.efi"}
    assert arch.verdict == VERDICT_UNSURE  # disk0-p5 tidak terbaca isinya, jadi bisa saja root-nya


def test_oem_folder_is_not_treated_as_linux(tmp_path):
    files = laptop_esp_files()
    files["EFI\\Lenovo\\Diagnostics.efi"] = b"MZ vendor tool"
    analysis = run(tmp_path, files, fsinfo={"disk0-p5": FsInfo("ext4", uuid=BOOT_UUID, last_mounted="/boot")})
    other = by_key(analysis, "other-esp")
    assert other.verdict == VERDICT_NOT_TARGET
    assert [c.ref for c in other.components] == ["EFI\\Lenovo"]


def test_fallback_identical_to_windows(tmp_path):
    files = {"EFI\\Microsoft\\Boot\\bootmgfw.efi": BOOTMGFW, "EFI\\Boot\\bootx64.efi": BOOTMGFW}
    analysis = run(tmp_path, files)
    assert analysis.fallback.same_as_windows
