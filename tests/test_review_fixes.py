"""Test untuk setiap temuan review Fable (2026-10-22). Nomor di nama test mengikuti laporan: K1 = jalur yang mengubah
sistem, K2 = deteksi & backup, K3 = Linux kit & ISO."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from distrorelay.common import profile as profile_mod
from distrorelay.windows import fsprobe, preflight, shrink
from distrorelay.windows.bootentries import BootEntry, FirmwareConfig, FirmwareEntry, merge_entries
from distrorelay.windows.bootrefs import (
    FsRef,
    VERDICT_LEFTOVER,
    VERDICT_NOT_TARGET,
    VERDICT_UNSURE,
    _Context,
    _prefix_refs,
    collect_esp_refs,
    fallback_kind,
    fallback_owner,
    partition_role,
)
from distrorelay.windows.disks import DiskInventory
from distrorelay.windows.execute import BOOTMGFW, ExecutionError, safe_esp_path
from distrorelay.windows.fsprobe import FsInfo
from distrorelay.windows.iso import manager, verify
from distrorelay.windows.journal import Journal, incomplete_sessions, read_journal, sessions
from distrorelay.windows.nvram import snapshot_from_raw
from distrorelay.windows.postcheck import post_check
from distrorelay.windows.runner import (
    ALLOWED_CMDLETS,
    FakeRunner,
    ReadOnlyViolation,
    check_powershell,
    powershell_args,
)
from distrorelay.windows.signature import signature_script
from distrorelay.windows.stopping import StopSignal
from distrorelay.windows.uninstall_plan import PlannedAction, UninstallPlan, build_plan, expand_selection
from distrorelay.windows.workflow import EspMountError, FlowError, RestoreFlow
from distrorelay_linux import backup as linux_backup
from distrorelay_linux import dotfiles, kit as linux_kit, pm, provision
from helpers import (
    ESP_GUID,
    file_node,
    gpt_slots_like_windows,
    grub_core_image,
    hd_node,
    laptop_inventory,
    laptop_real_esp_files,
    load_option,
)
from test_laptop_real import FSINFO, analyze_laptop, by_key, real_entries
from test_linux_backup import FEDORA, make_kit, make_home, make_runner, run_backup
from test_restore_flow import uninstalled
from test_workflow import make_flow, ready_for_gate

POSIX = os.name == "posix"


def _symlink(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlink tidak bisa dibuat di sistem ini (Windows tanpa Developer Mode)")


# --- K1-1: path dari disk tidak pernah masuk ke kode PowerShell -------------------------------------------

EVIL = "E:\\Win11\u2019;[IO.File]::Delete('C:\\x');\u2019.iso"


def test_k1_1_paths_are_sent_as_base64_not_as_code():
    for script in (preflight._script(preflight.ISO_MOUNT_SCRIPT, EVIL), signature_script(EVIL)):
        assert EVIL not in script and "\u2019" not in script
        check_powershell(script, ALLOWED_CMDLETS | preflight.M2A_CMDLETS)
        encoded = re.search(r"FromBase64String\('([^']+)'\)", script).group(1)
        assert base64.b64decode(encoded).decode("utf-8") == EVIL


@pytest.mark.parametrize("script", [
    "Get-Volume -FileSystemLabel '\u2019x\u2019'",
    "Get-Volume; [IO.File]::Delete('C:\\x')",
    "Get-Volume; [System.IO.Directory]::Delete('C:\\x', $true)",
])
def test_k1_1_guard_rejects_typographic_quotes_and_dotnet_calls(script):
    with pytest.raises(ReadOnlyViolation):
        check_powershell(script)


# --- K1-2: ID backup, salinan eksternal yang sama dengan lokal, manifest berbeda ------------------------------


def test_k1_2_gate_refuses_a_path_instead_of_a_backup_id(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    backup = ready_for_gate(flow, ext)
    flow.use_backup(str(tmp_path / "local" / backup.backup_id))
    gate = flow.gate()
    assert not gate.ok and any("tidak valid" in p and "bukan path" in p for p in gate.problems)


def test_k1_2_external_copy_must_be_the_same_backup(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    backup = ready_for_gate(flow, ext)
    manifest = ext / "DistroRelay" / "backups" / backup.backup_id / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    gate = flow.gate()
    assert not gate.ok and any("bukan salinan backup lokal ini" in p for p in gate.problems)


def test_k1_2_local_copy_never_counts_as_the_external_copy(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    drive_c = tmp_path / "C"
    flow.deps.backups_root = drive_c / "DistroRelay" / "backups"
    backup = ready_for_gate(flow, ext)
    flow.deps.external_roots = lambda report: [drive_c]  # "drive eksternal" yang ternyata folder backup lokal
    flow.use_backup(backup.backup_id)
    gate = flow.gate()
    assert not gate.ok and any("drive eksternal tidak ditemukan" in p for p in gate.problems)


# --- K1-3: sesi hanya dituntaskan sesi sejenis; nama sesi unik ------------------------------------------------


def test_k1_3_only_a_later_session_of_the_same_kind_resolves_an_interrupted_one(tmp_path):
    cut = Journal.start(tmp_path, "20261022-100000", {"kind": "uninstall"})
    cut.record(1, "delete-partition", "disk0-p5", "started")  # terputus di tengah langkah
    shrunk = Journal.start(tmp_path, "20261022-110000", {"kind": "shrink-c"})
    shrunk.post_check(True, [])
    shrunk.finish(True)
    assert [s.path.stem for s in incomplete_sessions(tmp_path)] == ["20261022-100000"]
    later = Journal.start(tmp_path, "20261022-120000", {"kind": "uninstall"})
    later.post_check(True, [])
    later.finish(True)
    assert incomplete_sessions(tmp_path) == []


def test_k1_3_sessions_in_the_same_second_get_unique_names_in_order(tmp_path):
    first = Journal.start(tmp_path, "20261022-100000", {"kind": "uninstall"})
    second = Journal.start(tmp_path, "20261022-100000", {"kind": "restore"})
    assert first.path != second.path and second.path.name == "20261022-100000_2.jsonl"
    assert [s.kind for s in sessions(tmp_path)] == ["uninstall", "restore"]


# --- K1-4: restore dicatat di journal dan bisa berhenti; shrink tahan Ctrl+C dan timeout ---------------------


def test_k1_4_restore_is_journaled_and_stops_between_steps(tmp_path):
    flow, bcd, backup_id = uninstalled(tmp_path)
    restore = RestoreFlow(flow.deps)
    assert all(v.ok for v in restore.load(backup_id))
    fedora = next(c for c in restore.entry_choices() if "'Fedora'" in c.label)

    stop = StopSignal()
    stop.request("Dihentikan pengguna (Ctrl+C)")
    assert restore.select(["EFI\\fedora"], [fedora.key]) is None
    results = restore.run(restore.authorize("PULIHKAN"), stop=stop)
    assert "sebelum langkah pertama" in results[-1].message and not (tmp_path / "esp" / "EFI" / "fedora").exists()
    # Tidak ada yang dijalankan: tidak ada sesi restore yang "belum tuntas".
    assert [s for s in sessions(tmp_path / "journal") if s.kind == "restore"] == []

    assert restore.select(["EFI\\fedora"], [fedora.key]) is None
    results = restore.run(restore.authorize("PULIHKAN"))
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    done = [s for s in sessions(tmp_path / "journal") if s.kind == "restore"][-1]
    assert done.ok and done.post_check_ok
    kinds = [e.kind for e in read_journal(done.path) if e.status == "done"]
    assert kinds == ["restore-esp", "restore-boot-entry"]
    assert incomplete_sessions(tmp_path / "journal") == []


C_GUID = "{c3b2a1d0-1111-4222-8333-444455556666}"
C_INFO = {"DiskNumber": 0, "PartitionNumber": 3, "DiskGuid": "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}", "Guid": C_GUID,
          "Offset": 290455552, "Size": 300 * 2**30, "SizeMin": 120 * 2**30, "SizeMax": 300 * 2**30,
          "Free": 150 * 2**30, "FileSystem": "NTFS"}


def _shrink_plan():
    return shrink.plan_shrink(shrink.parse_query(json.dumps(C_INFO)), 60 * 2**30)


def test_k1_4_shrink_timeout_is_recorded_as_unknown_state(tmp_path):
    runner = FakeRunner()
    runner.add("ps:shrink-query", json.dumps(C_INFO))

    def hang(args):
        raise subprocess.TimeoutExpired(list(args), 1800)

    runner.add("ps:shrink-c", "", effect=hang)
    result = shrink.execute(_shrink_plan(), runner, journal_root=tmp_path)
    assert not result.ok and "STATUS TIDAK DIKETAHUI" in result.message
    [session] = sessions(tmp_path)
    assert session.kind == "shrink-c" and session.finished and not session.ok


def test_k1_4_shrink_stop_before_start_changes_nothing(tmp_path):
    runner = FakeRunner()
    stop = StopSignal()
    stop.request("Dihentikan pengguna (Ctrl+C)")
    result = shrink.execute(_shrink_plan(), runner, journal_root=tmp_path, stop=stop)
    assert not result.ok and "tidak ada yang diubah" in result.message and runner.calls == []


# --- K1-5: shrink mengecek ulang ruang kosong di dalam script ------------------------------------------------


def test_k1_5_shrink_script_rechecks_free_space_and_windows_limit():
    plan = _shrink_plan()
    assert "Get-PartitionSupportedSize" in plan.script and "Get-Volume -DriveLetter C" in plan.script
    assert f"($v.SizeRemaining - {plan.shrink_by}) -lt {shrink.MIN_FREE_AFTER}" in plan.script
    assert f"{plan.new_size} -lt $s.SizeMin" in plan.script
    assert plan.script.index("$v.SizeRemaining") < plan.script.index("Resize-Partition")


# --- K1-6: fallback Linux yang tidak identik tetap dikembalikan ke Windows bila perlu --------------------------


def _plan(tmp_path, files, *selectors):
    inventory = laptop_inventory()
    analysis, _ = analyze_laptop(tmp_path, files, inventory=inventory)
    refs, unknown = expand_selection(analysis, list(selectors))
    assert unknown == []
    return analysis, build_plan(analysis, real_entries()[0], refs, inventory=inventory, fsinfo=FSINFO)


def test_k1_6_grub_fallback_that_loads_from_a_deleted_folder_is_restored(tmp_path):
    files = laptop_real_esp_files()
    files["EFI\\Boot\\bootx64.efi"] = grub_core_image(prefix="(,gpt1)/EFI/Arch")
    analysis, plan = _plan(tmp_path, files, "efi:arch")
    assert analysis.fallback.kind == "loader" and analysis.fallback.loader_dir.lower() == "efi\\arch"
    action = next(a for a in plan.actions if a.kind == "restore-fallback")
    assert any("memuat modul/config dari" in d for d in action.details)

    _, other = _plan(tmp_path / "b", files, "efi:fedora")  # EFI\Arch tetap ada: fallback dibiarkan
    assert not any(a.kind == "restore-fallback" for a in other.actions)
    assert any("dibiarkan" in w and "EFI\\Arch" in w for w in other.warnings)


def test_k1_6_linux_fallback_is_restored_when_no_linux_folder_remains(tmp_path):
    files = laptop_real_esp_files()
    files["EFI\\Boot\\bootx64.efi"] = b"MZ UEFI SHIM versi lain"
    _, plan = _plan(tmp_path, files, "efi:fedora", "efi:arch", "grubdir:grub")
    action = next(a for a in plan.actions if a.kind == "restore-fallback")
    assert any("tidak ada folder Linux lain" in d for d in action.details)


def test_k1_6_fallback_owner_markers():
    assert fallback_kind(fallback_owner(b"MZ ... bootmgfw.pdb ... Microsoft Corporation")) == "windows"
    assert fallback_owner(b"MZ UEFI SHIM ... Microsoft Corporation UEFI CA") == "shim"
    assert fallback_kind(fallback_owner(b"MZ sesuatu")) == "unknown"


# --- K1-7: tiket tidak bisa diterbitkan ulang setelah eksekusi; K1-9: journal & post-check --------------------


def test_k1_7_no_second_ticket_after_execution(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    assert flow.gate().ok
    assert flow.execute(flow.authorize("HAPUS")).completed
    with pytest.raises(FlowError):
        flow.authorize("HAPUS")
    assert all(c.ok for c in flow.post_check())


def test_k1_9_esp_mount_failure_is_failed_not_interrupted(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    assert flow.gate().ok
    ticket = flow.authorize("HAPUS")

    @contextmanager
    def broken(runner, partition):
        raise OSError("mountvol gagal")
        yield  # pragma: no cover

    flow.deps.mount_esp = broken
    with pytest.raises(EspMountError):
        flow.execute(ticket)
    [session] = sessions(tmp_path / "journal")
    assert session.finished and not session.ok and "tidak ada yang diubah" in session.end_message
    assert session.changed is False and incomplete_sessions(tmp_path / "journal") == []


def test_k1_9_timeout_of_a_changing_command_is_reported_as_unknown(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    ready_for_gate(flow, ext)
    assert flow.gate().ok
    original = bcd.run

    def run(args, timeout=120):
        if list(args[:2]) == ["bcdedit", "/set"]:
            raise subprocess.TimeoutExpired(list(args), timeout)
        return original(args, timeout=timeout)

    bcd.run = run
    result = flow.execute(flow.authorize("HAPUS"))
    assert not result.completed and "STATUS TIDAK DIKETAHUI" in result.stopped


def _entry(key, description, path, bcd_id, order):
    return BootEntry(key, description, path, None, bcd_id, None, order, True, ["NVRAM", "bcdedit"], None)


def test_k1_9_post_check_counts_duplicate_entries():
    wbm = _entry("nv:0000", "Windows Boot Manager", "\\EFI\\Microsoft\\Boot\\bootmgfw.efi",
                 "{9dea862c-5cdd-4e70-acc1-f32b344d4795}", 0)
    first = _entry("nv:0001", "Fedora", "\\EFI\\fedora\\shimx64.efi", "{aaaaaaaa-0000-4000-8000-000000000001}", 1)
    twin = _entry("nv:0002", "Fedora", "\\EFI\\fedora\\shimx64.efi", "{aaaaaaaa-0000-4000-8000-000000000002}", 2)
    plan = UninstallPlan(selected=[], actions=[PlannedAction(
        "delete-boot-entry", "Hapus", [], data={"bcd_id": first.bcd_id, "description": "Fedora", "path": first.path})])
    files = [SimpleNamespace(path=BOOTMGFW, sha256="h")]
    esp = SimpleNamespace(files=files, has_dir=lambda p: False,
                          find=lambda p: next((f for f in files if f.path.lower() == p.lower()), None))
    manifest = {"esp_files": [{"path": BOOTMGFW, "sha256": "h"}]}
    before = SimpleNamespace(boot_entries=[wbm, first, twin], inventory=laptop_inventory(), esp=esp)

    after = SimpleNamespace(boot_entries=[wbm, twin], inventory=laptop_inventory(), esp=esp)
    checks = {c.name: c for c in post_check(plan, manifest, before, after)}
    assert checks["Entri yang dihapus sudah tidak ada"].ok and checks["Entri lain tetap ada"].ok

    after = SimpleNamespace(boot_entries=[wbm], inventory=laptop_inventory(), esp=esp)
    checks = {c.name: c for c in post_check(plan, manifest, before, after)}
    assert not checks["Entri lain tetap ada"].ok


# --- K1-8: folder alat pabrikan tidak pernah diubah ----------------------------------------------------------


@pytest.mark.parametrize("rel", ["EFI\\HP\\BIOS\\x.bin", "EFI\\Dell\\x.efi", "efi\\lenovo"])
def test_k1_8_vendor_folders_are_never_touched(tmp_path, rel):
    with pytest.raises(ExecutionError):
        safe_esp_path(tmp_path, rel)


# --- K2-1, K2-4: entri di partisi lain, folder tak dikenal, inventaris kosong ---------------------------------


def test_k2_1_entry_on_another_disk_is_not_merged_with_the_esp_folder(tmp_path):
    inventory = laptop_inventory()
    files = laptop_real_esp_files()
    from helpers import add_partition, fake_esp, nvram_raw_laptop
    other_esp = add_partition(inventory, 6, "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "{0e5a0000-0000-4000-8000-0000000000e5}")
    raw = nvram_raw_laptop()
    raw["Boot0001"] = load_option("Fedora", [hd_node(6, other_esp.guid.strip("{}")), file_node("\\EFI\\fedora\\shimx64.efi")]).hex()
    esp = fake_esp(tmp_path, inventory, files)
    entries, notes = merge_entries(None, snapshot_from_raw(raw))
    from distrorelay.windows.bootrefs import analyze
    analysis = analyze(inventory, FSINFO, True, entries, esp, collect_esp_refs(tmp_path, esp), notes,
                       gpt_slots=gpt_slots_like_windows(inventory))
    elsewhere = by_key(analysis, "efi:fedora@disk0-p6")
    assert elsewhere.verdict == VERDICT_UNSURE and "nv:0001" in {c.ref for c in elsewhere.components}
    assert any("tidak dianalisis" in r for r in elsewhere.reasons)
    assert "nv:0001" not in {c.ref for c in by_key(analysis, "efi:fedora").components}


def test_k2_4_unknown_folder_names_are_not_linux_and_vendor_entries_stay_protected(tmp_path):
    from helpers import fake_esp, nvram_raw_laptop
    from distrorelay.windows.bootrefs import analyze
    inventory = laptop_inventory()
    raw = nvram_raw_laptop()
    esp_guid = ESP_GUID
    raw["Boot0001"] = load_option("Alat", [hd_node(1, esp_guid), file_node("\\EFI\\toolbox\\x.efi")]).hex()
    raw["Boot0002"] = load_option("HP Tools", [hd_node(1, esp_guid), file_node("\\EFI\\HP\\Missing\\y.efi")]).hex()
    files = {"EFI\\Microsoft\\Boot\\bootmgfw.efi": b"MZ bootmgfw.pdb"}
    esp = fake_esp(tmp_path, inventory, files)
    entries, notes = merge_entries(None, snapshot_from_raw(raw))
    analysis = analyze(inventory, {}, True, entries, esp, collect_esp_refs(tmp_path, esp), notes)
    toolbox = by_key(analysis, "efi:toolbox")
    assert toolbox.kind == "unattached" and toolbox.verdict == VERDICT_UNSURE
    hp = by_key(analysis, "other-esp")
    assert hp.verdict == VERDICT_NOT_TARGET and "nv:0002" in {c.ref for c in hp.components}


def test_k2_4_missing_inventory_makes_references_unknown_not_missing():
    ctx = _Context(DiskInventory(), {}, True, None)
    assert ctx.resolve(FsRef("partuuid", "{12345678-0000-4000-8000-000000000000}", "x"))[1] == "tidak diketahui"
    assert ctx.resolve(FsRef("fs-uuid", "abcd", "x"))[1] == "tidak diketahui"


# --- K2-2: path mount yang tidak dikenal bisa root -------------------------------------------------------------


@pytest.mark.parametrize("mounted, role", [
    ("/", "root"), ("/mnt", "mungkin-root"), ("/sysroot", "mungkin-root"), ("/new_root", "mungkin-root"),
    ("/var", "data"), ("/run/media/user/DATA", "mungkin-root"), ("/media/ubuntu/abc", "mungkin-root"),
    ("/home", "home"), ("/data/foto", "data"),
])
def test_k2_2_partition_role_by_last_mount(mounted, role):
    import dataclasses
    part = next(p for p in laptop_inventory().partitions if p.number == 5)
    part = dataclasses.replace(part, gpt_type="0fc63daf-8483-4772-8e79-3d69d8477de4", category="linux")
    assert partition_role(part, {part.id: FsInfo("ext4", uuid="u", last_mounted=mounted)}) == role


# --- K2-3: slot GPT dibaca dari tabel partisi ------------------------------------------------------------------


def _gpt(entries: dict[int, str], count: int = 8, size: int = 128) -> tuple[bytes, bytes]:
    import zlib
    table = bytearray(count * size)
    for slot, guid in entries.items():
        offset = (slot - 1) * size
        table[offset:offset + 16] = uuid.UUID("0fc63daf-8483-4772-8e79-3d69d8477de4").bytes_le
        table[offset + 16:offset + 32] = uuid.UUID(guid.strip("{}")).bytes_le
    header = bytearray(512)
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<QIII", header, 72, 2, count, size, zlib.crc32(bytes(table)))
    struct.pack_into("<I", header, 16, zlib.crc32(bytes(header[:92])))
    return bytes(header), bytes(table)


def test_k2_3_gpt_slots_keep_gaps():
    a, b = "11111111-0000-4000-8000-000000000001", "22222222-0000-4000-8000-000000000002"
    slots = fsprobe.parse_gpt_slots(*_gpt({1: a, 4: b}))
    assert slots == {1: "{" + a + "}", 4: "{" + b + "}"}
    with pytest.raises(ValueError):
        fsprobe.parse_gpt_slots(b"NOT GPT" + bytes(100), b"")


@pytest.mark.parametrize("damage", ["header", "array", "truncated", "huge"])
def test_r2_gpt_table_is_verified_before_use(damage):
    header, table = _gpt({1: "11111111-0000-4000-8000-000000000001", 6: "22222222-0000-4000-8000-000000000002"})
    header, table = bytearray(header), bytearray(table)
    if damage == "header":
        header[80] ^= 1  # jumlah entri berubah, CRC header tidak cocok
    elif damage == "array":
        table[5 * 128 + 20] ^= 1
    elif damage == "truncated":
        table = table[: 4 * 128]  # slot 6 tidak terbaca: jangan dianggap kosong
    else:
        struct.pack_into("<I", header, 84, 0xFFFFFFF0)
    with pytest.raises(ValueError):
        fsprobe.parse_gpt_slots(bytes(header), bytes(table))


def test_k2_3_grub_slot_is_unknown_without_the_gpt_table_and_on_other_disks(tmp_path):
    analysis, _ = analyze_laptop(tmp_path)
    assert by_key(analysis, "efi:arch").verdict == VERDICT_LEFTOVER  # slot 4 = Windows Recovery (dari tabel GPT)
    from distrorelay.windows.bootrefs import analyze
    from helpers import fake_esp
    inventory = laptop_inventory()
    esp = fake_esp(tmp_path / "b", inventory, laptop_real_esp_files())
    entries, notes = real_entries()
    no_table = analyze(inventory, FSINFO, True, entries, esp, collect_esp_refs(tmp_path / "b", esp), notes)
    arch = by_key(no_table, "efi:arch")
    assert arch.verdict == VERDICT_UNSURE and any("tidak bisa dicek" in r for r in arch.reasons)
    assert _prefix_refs("(hd1,gpt3)/grub", "x", 1)[0].kind == "partnum-other-disk"


def test_k2_3_gap_in_the_gpt_table_changes_what_gptN_means(tmp_path):
    inventory = laptop_inventory()
    slots = gpt_slots_like_windows(inventory)
    disk = slots[0]
    disk[4], disk[5] = disk[5], disk[4]  # urutan array GPT tidak harus sama dengan nomor partisi Windows
    from distrorelay.windows.bootrefs import analyze
    from helpers import fake_esp
    esp = fake_esp(tmp_path, inventory, laptop_real_esp_files())
    entries, notes = real_entries()
    analysis = analyze(inventory, FSINFO, True, entries, esp, collect_esp_refs(tmp_path, esp), notes, gpt_slots=slots)
    arch = by_key(analysis, "efi:arch")
    # (,gpt4) sekarang menunjuk partisi Linux disk0-p5, bukan Windows Recovery.
    assert not any("bukan partisi Linux" in r for r in arch.reasons)
    assert any("disk0-p5" in r for r in arch.reasons)


# --- K2-5: pasangan NVRAM-bcdedit yang ambigu tidak ditebak ----------------------------------------------------


def test_k2_5_identical_bcd_objects_are_not_guessed():
    path = "\\EFI\\fedora\\shimx64.efi"
    raw = {
        "BootOrder": struct.pack("<HH", 1, 2).hex(),
        "Boot0001": load_option("Fedora", [hd_node(1, ESP_GUID), file_node(path)]).hex(),
        "Boot0002": load_option("Fedora", [hd_node(1, "99999999-0000-4000-8000-000000000009"), file_node(path)]).hex(),
    }
    fw = FirmwareConfig(display_order=[], entries=[
        FirmwareEntry("{aaaaaaaa-0000-4000-8000-000000000001}", "Fedora", path, None, "x"),
        FirmwareEntry("{aaaaaaaa-0000-4000-8000-000000000002}", "Fedora", path, None, "x"),
    ])
    entries, notes = merge_entries(fw, snapshot_from_raw(raw))
    nv = [e for e in entries if e.key.startswith("nv:")]
    assert all(e.bcd_id is None and not e.confirmed for e in nv)
    assert all(any("tidak bisa dipastikan" in c for c in e.conflicts) for e in nv)
    assert not any("terlihat di NVRAM tapi tidak di bcdedit" in n or "terlihat di bcdedit tapi tidak di NVRAM" in n
                   for n in notes)
    assert sum("punya kembaran di bcdedit" in n for n in notes) == 2


# --- K2-6/7: verifikasi USB dibaca dari perangkat; file ESP yang tidak tercadang --------------------------------


def test_k2_6_uncached_hash_matches(tmp_path):
    from distrorelay.windows.uncached import sha256_uncached
    for size in (0, 1, 4095, 1024 * 1024 + 7):
        path = tmp_path / f"f{size}"
        data = os.urandom(size)
        path.write_bytes(data)
        assert sha256_uncached(path) == hashlib.sha256(data).hexdigest()


def test_k2_7_locked_file_pattern():
    from distrorelay.windows.backup import _LOCKED_ESP_FILE, valid_backup_id
    assert _LOCKED_ESP_FILE.match("EFI\\Microsoft\\Boot\\BCD") and _LOCKED_ESP_FILE.match("efi\\microsoft\\boot\\bcd.log2")
    assert not _LOCKED_ESP_FILE.match("EFI\\fedora\\grub.cfg")
    assert valid_backup_id("20261022-153000") and not valid_backup_id("C:\\x\\20261022-153000")


# --- K3-1: file sensitif tidak bocor lewat symlink/hardlink ----------------------------------------------------


def _names(buffer: io.BytesIO) -> list[str]:
    buffer.seek(0)
    with tarfile.open(fileobj=buffer, mode="r:gz") as tar:
        return tar.getnames()


def test_k3_1_hardlinked_secret_does_not_enter_the_plain_archive(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / "dotfiles").mkdir()
    (home / ".ssh" / "id_ed25519").write_bytes(b"RAHASIA")
    (home / "dotfiles" / "rc").write_bytes(b"ok")
    os.link(home / ".ssh" / "id_ed25519", home / "dotfiles" / "key")
    buffer = io.BytesIO()
    report = dotfiles.write_archive(buffer, home, ["dotfiles"], exclude=[".ssh"])
    assert "dotfiles/key" not in _names(buffer) and "dotfiles/rc" in _names(buffer)
    assert ("dotfiles/key", "sensitif, hanya dibackup terenkripsi") in report.skipped


def test_k3_1_symlinked_secret_dir_is_excluded_and_followed_only_when_encrypting(tmp_path):
    home = tmp_path / "home"
    (home / "dotfiles" / "ssh").mkdir(parents=True)
    (home / "dotfiles" / "ssh" / "id_ed25519").write_bytes(b"RAHASIA")
    _symlink(home / "dotfiles" / "ssh", home / ".ssh")
    plain = io.BytesIO()
    dotfiles.write_archive(plain, home, ["dotfiles"], exclude=[".ssh"])
    assert not any("id_ed25519" in n for n in _names(plain))
    secret = io.BytesIO()
    report = dotfiles.write_archive(secret, home, [".ssh"], follow_symlinks=True)
    assert ".ssh/id_ed25519" in _names(secret) and report.followed


# --- K3-2: apt tidak boleh menghapus paket ---------------------------------------------------------------------


def test_k3_2_apt_install_never_removes_packages():
    command = pm.backend_for("debian").install(["pipewire-pulse"], True)
    assert "--no-remove" in command.args


# --- K3-3 (no. 13): dicek di test_linux_backup.test_verified_linux_backup_does_not_replace_the_data_statement -----


# --- K3-4/5/7: restore file sensitif, .bak unik, satu member rusak tidak menghentikan semuanya --------------------


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_k3_5_existing_backup_copies_are_never_overwritten(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_bytes(b"asli")
    (home / ".bashrc.distrorelay-S.bak").write_bytes(b"bak lama")
    report = dotfiles.restore(_tar({".bashrc": b"baru"}), home, "S")
    assert (home / ".bashrc").read_bytes() == b"baru"
    assert (home / ".bashrc.distrorelay-S.bak").read_bytes() == b"bak lama"
    assert (home / ".bashrc.distrorelay-S-2.bak").read_bytes() == b"asli"
    assert report.renamed == [(".bashrc", ".bashrc.distrorelay-S-2.bak")]


def test_k3_5_overlapping_profile_paths_are_archived_once(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "nvim").mkdir(parents=True)
    (home / ".config" / "nvim" / "init.lua").write_bytes(b"x")
    buffer = io.BytesIO()
    dotfiles.write_archive(buffer, home, [".config", ".config/nvim"])
    names = _names(buffer)
    assert names.count(".config/nvim/init.lua") == 1 and len(names) == len(set(names))


def test_k3_7_one_bad_member_does_not_stop_the_restore(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "a").write_bytes(b"file biasa, bukan folder")
    report = dotfiles.restore(_tar({"a/b": b"x", ".bashrc": b"ok"}), home, "S")
    assert (home / ".bashrc").read_bytes() == b"ok"
    assert [rel for rel, _ in report.skipped] == ["a/b"]


@pytest.mark.skipif(not POSIX, reason="permission POSIX")
def test_k3_4_private_restore_creates_700_parents_and_fixes_unchanged_files(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    report = dotfiles.restore(_tar({".ssh/id_ed25519": b"k"}), home, "S", private=True)
    assert report.restored == [".ssh/id_ed25519"]
    assert (home / ".ssh").stat().st_mode & 0o777 == 0o700
    os.chmod(home / ".ssh" / "id_ed25519", 0o644)
    dotfiles.restore(_tar({".ssh/id_ed25519": b"k"}), home, "S", private=True)
    assert (home / ".ssh" / "id_ed25519").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(not POSIX, reason="symlink POSIX")
def test_k3_4_symlink_in_the_target_path_is_not_followed(tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "luar"
    home.mkdir()
    outside.mkdir()
    (home / ".ssh").symlink_to(outside)
    report = dotfiles.restore(_tar({".ssh/id_ed25519": b"k"}), home, "S", private=True)
    assert not (outside / "id_ed25519").exists() and report.skipped


# --- K3-6, K3-9: gpg biner; file yang tidak tercatat di manifest ----------------------------------------------


def test_k3_6_encrypt_forces_binary_output():
    seen = {}

    class Runner:
        def run_bytes(self, cmd, data=b""):
            seen["args"] = cmd.args
            return SimpleNamespace(ok=True, returncode=0, data=b"\x8c\x0d" + data)

    linux_backup.encrypt(Runner(), b"x")
    assert "--no-armor" in seen["args"]


def test_k3_9_unlisted_files_and_missing_core_entries_are_rejected(tmp_path):
    kit, result = run_backup(tmp_path, runner=make_runner(usb=False), include_sensitive=False)
    manifest_path = result.path / linux_backup.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [f for f in manifest["files"] if f["path"] != linux_backup.DOTFILES]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any("tidak tercatat di manifest" in p for p in linux_backup.verify(result.path))

    kit_manifest = kit.root / linux_kit.MANIFEST
    data = json.loads(kit_manifest.read_text(encoding="utf-8"))
    data["files"] = [f for f in data["files"] if f["path"] != "profile.json"]
    kit_manifest.write_text(json.dumps(data), encoding="utf-8")
    assert any("profile.json: tidak tercatat" in p for p in linux_kit.verify(kit.root))


# --- K3-8: setup sebagai root tidak menulis dotfiles ke /root -------------------------------------------------


def test_k3_8_setup_as_root_skips_dotfiles(tmp_path):
    kit, result = run_backup(tmp_path, runner=make_runner(usb=False), include_sensitive=False)
    plan = provision.build_plan(kit.profile, kit.packages, FEDORA, is_root=True, has_paru=False, home=tmp_path / "root",
                                stamp="S", workdir=str(tmp_path / "w"), backup_dir=result.path,
                                read_file=lambda path: "")
    assert not any(step.key == "dotfiles" for step in plan.steps)
    assert any("sebagai root" in note for note in plan.notes)


# --- K3-10/11/12: ISO ---------------------------------------------------------------------------------------------


def test_k3_10_copy_never_overwrites_a_file_that_appears_meanwhile(tmp_path):
    source = tmp_path / "src.iso"
    source.write_bytes(b"ISO" * 100_000)
    usb = tmp_path / "usb"
    usb.mkdir()
    sha = hashlib.sha256(source.read_bytes()).hexdigest()

    def appear(done, total):
        target = usb / "src.iso"
        if not target.exists():
            target.write_bytes(b"punya orang lain")

    steps = manager.copy_verified(source, usb, sha, progress=appear)
    assert not steps[0].ok and "muncul selama penyalinan" in steps[0].message
    assert (usb / "src.iso").read_bytes() == b"punya orang lain"
    assert not list(usb.glob("*" + manager.PART_SUFFIX))


VALID = "[GNUPG:] GOODSIG X Test\n[GNUPG:] VALIDSIG {fpr} 2026-01-01 1 0 4 0 {algo} 10 00 {fpr}"


def test_k3_11_and_12_only_badsig_marks_an_iso_bad_and_weak_keys_are_refused(tmp_path):
    from test_iso import TEST_FPR, TEST_KEYS
    data = tmp_path / "d"
    data.write_bytes(b"x")

    def judge(stdout, code=0):
        return verify.check_signature(TEST_KEYS, data, data, gpgv="gpgv",
                                      run=lambda args, cwd: SimpleNamespace(ok=code == 0, returncode=code,
                                                                            stdout=stdout, output=lambda: ""))

    assert "kunci lemah" in judge(VALID.format(fpr=TEST_FPR, algo=17)).message
    assert judge(VALID.format(fpr=TEST_FPR, algo=22)).ok
    bad = judge("[GNUPG:] BADSIG X Test", 1)
    assert bad.bad_signature and not bad.ok
    unknown_key = judge("[GNUPG:] ERRSIG X 1 10 00 1 9 X\n[GNUPG:] NO_PUBKEY X", 2)
    assert unknown_key.judged and not unknown_key.bad_signature


# --- Review ulang Fable, kelompok 3 ----------------------------------------------------------------------------


def test_r2_b1_per_file_symlinks_inside_a_sensitive_folder(tmp_path):
    """GNU stow: ~/.ssh nyata, isinya symlink per file ke ~/dotfiles. Kuncinya tidak boleh masuk arsip biasa, dan
    harus ikut di arsip terenkripsi."""
    home = tmp_path / "home"
    (home / "dotfiles" / "ssh").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / "dotfiles" / "ssh" / "id_ed25519").write_bytes(b"RAHASIA")
    (home / "dotfiles" / "rc").write_bytes(b"ok")
    _symlink(home / "dotfiles" / "ssh" / "id_ed25519", home / ".ssh" / "id_ed25519")
    plain = io.BytesIO()
    dotfiles.write_archive(plain, home, ["dotfiles"], exclude=[".ssh"])
    assert not any("id_ed25519" in n for n in _names(plain)) and "dotfiles/rc" in _names(plain)
    secret = io.BytesIO()
    report = dotfiles.write_archive(secret, home, [".ssh"], follow_symlinks=True)
    secret.seek(0)
    with tarfile.open(fileobj=secret, mode="r:gz") as tar:
        assert tar.extractfile(".ssh/id_ed25519").read() == b"RAHASIA"
    assert report.followed


def test_r2_b3_restore_outcome_names_what_was_skipped():
    report = dotfiles.RestoreReport(restored=[".bashrc"], skipped=[(".ssh/id", "ada symlink di path tujuan")])
    status, message = provision.restore_outcome(report)
    assert status == "sebagian" and ".ssh/id: ada symlink di path tujuan" in message
    assert provision.restore_outcome(dotfiles.RestoreReport(skipped=[("a", "gagal: disk penuh")]))[0] == "gagal"
    assert provision.restore_outcome(dotfiles.RestoreReport(restored=["a"]))[0] == "ok"


def test_r2_b4_root_setup_notes_do_not_contradict(tmp_path):
    kit, result = run_backup(tmp_path, runner=make_runner(usb=False), include_sensitive=False)
    plan = provision.build_plan(kit.profile, kit.packages, FEDORA, is_root=True, has_paru=False, home=tmp_path / "root",
                                stamp="S", workdir=str(tmp_path / "w"), backup_dir=result.path,
                                read_file=lambda path: "")
    assert not any("belum ada backup" in n or "tidak ada salinan terenkripsinya" in n for n in plan.notes)


def test_r2_b6_foreign_code_in_the_kit_folder_is_rejected(tmp_path):
    kit = make_kit(tmp_path / "usb")
    (kit.root / "distrorelay_linux" / "extra.py").write_text("import os\n", encoding="utf-8")
    (kit.root / "distrorelay_linux" / "__pycache__").mkdir(exist_ok=True)
    (kit.root / "distrorelay_linux" / "__pycache__" / "x.py").write_text("", encoding="utf-8")
    problems = linux_kit.verify(kit.root)
    assert problems == ["distrorelay_linux/extra.py: ada di folder kit tapi tidak tercatat di manifest"]



# --- Review ulang Fable, kelompok 1 ----------------------------------------------------------------------------


def test_r2_k1_b1_fallback_grub_loading_from_a_live_partition_is_left_alone(tmp_path):
    """grub-install --removable dengan prefix (,gpt5): fallback dipakai Linux di partisi #5. Menghapus folder lain
    tidak boleh memaksa fallback diganti; menghapus partisi #5-nya ikut mewajibkan penggantian."""
    files = laptop_real_esp_files()
    files["EFI\\Boot\\bootx64.efi"] = grub_core_image(prefix="(,gpt5)/boot/grub")
    analysis, plan = _plan(tmp_path, files, "EFI\\Arch", "nv:0004")
    assert analysis.fallback.loader_partnum == 5 and analysis.fallback.loader_target_known
    assert not any(a.kind == "restore-fallback" for a in plan.actions)
    assert any("memuat dari partisi/disk lain" in w for w in plan.warnings)
    _, with_partition = _plan(tmp_path / "b", files, "efi:fedora")  # termasuk disk0-p5
    action = next(a for a in with_partition.actions if a.kind == "restore-fallback")
    assert any("slot GPT 5" in d for d in action.details)


def test_r2_k1_b2_shrink_aborted_by_its_own_check_is_not_unfinished(tmp_path):
    runner = FakeRunner()
    runner.add("ps:shrink-query", json.dumps(C_INFO))
    runner.add("ps:shrink-c", "", returncode=shrink.UNCHANGED_EXIT,
               stderr="Ruang kosong C: atau batas Windows berubah sejak dicek; dibatalkan tanpa mengubah apa pun")
    result = shrink.execute(_shrink_plan(), runner, journal_root=tmp_path)
    assert not result.ok
    [session] = sessions(tmp_path)
    assert session.changed is False and incomplete_sessions(tmp_path) == []


def test_r2_k1_b3_journal_text_shows_the_kind(tmp_path):
    from distrorelay import render
    Journal.start(tmp_path, "20261022-100000", {"kind": "shrink-c"}).finish(True)
    assert "[shrink C:]" in render.journal_text(sessions(tmp_path)).plain


# --- Review ulang Fable: baca ulang dari perangkat gagal tertutup ------------------------------------------------


def test_r2_device_read_error_fails_the_copy_and_backup_checks(tmp_path, monkeypatch):
    source = tmp_path / "src.iso"
    source.write_bytes(b"ISO" * 1000)
    usb = tmp_path / "usb"
    usb.mkdir()
    sha = hashlib.sha256(source.read_bytes()).hexdigest()

    def broken(path, progress=None):
        raise OSError(23, "Data error (cyclic redundancy check)")

    monkeypatch.setattr(manager, "sha256_uncached", broken)
    steps = manager.copy_verified(source, usb, sha)
    assert steps[0].ok and not steps[1].ok and "salinan dihapus" in steps[1].message
    assert not (usb / "src.iso").exists()

    from distrorelay.windows.backup import BackupFile, verify_files
    (tmp_path / "f").write_bytes(b"x")
    check = verify_files(tmp_path, [BackupFile("f", 1, hashlib.sha256(b"x").hexdigest())], "USB", broken)
    assert not check.ok and "tidak terbaca" in check.message


def test_r2_uncached_reports_progress(tmp_path):
    from distrorelay.windows.uncached import sha256_uncached
    path = tmp_path / "f"
    path.write_bytes(b"a" * (3 * 1024 * 1024 + 5))
    seen = []
    sha256_uncached(path, lambda done, total: seen.append((done, total)))
    assert seen[-1] == (path.stat().st_size, path.stat().st_size) and len(seen) >= 4



# --- Pemeriksaan akhir Fable -------------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["($root)/boot/grub", "(lvm/vg-root)/boot/grub", "(cryptouuid/abcd)/boot/grub"])
def test_final_m1_fallback_grub_on_lvm_or_luks_is_not_forced(tmp_path, prefix):
    files = laptop_real_esp_files()
    files["EFI" + chr(92) + "Boot" + chr(92) + "bootx64.efi"] = grub_core_image(prefix=prefix)
    analysis, plan = _plan(tmp_path, files, "efi:fedora", "efi:arch", "grubdir:grub")
    assert analysis.fallback.loader_unmapped and analysis.fallback.loader_dir is None
    assert not any(a.kind == "restore-fallback" for a in plan.actions)
    assert any("memuat dari partisi/disk lain" in w for w in plan.warnings)


def test_final_m1_fallback_grub_whose_root_filesystem_is_gone_is_restored(tmp_path):
    files = laptop_real_esp_files()
    config = "search --no-floppy --fs-uuid --set=root 99999999-0000-4000-8000-000000000099"
    files["EFI" + chr(92) + "Boot" + chr(92) + "bootx64.efi"] = grub_core_image(prefix="($root)/boot/grub", config=config)
    analysis, plan = _plan(tmp_path, files, "efi:fedora", "efi:arch", "grubdir:grub")
    assert analysis.fallback.loader_refs_gone
    action = next(a for a in plan.actions if a.kind == "restore-fallback")
    assert any("config GRUB-nya" in d for d in action.details)


def test_final_powershell_exit_code_reaches_the_runner():
    from distrorelay.windows.runner import SubprocessRunner
    if sys.platform != "win32":
        pytest.skip("PowerShell Windows")
    script = chr(10).join(["# distrorelay:test", "[Console]::Error.WriteLine('dibatalkan tanpa mengubah apa pun')", "exit 3"])
    result = SubprocessRunner().run(powershell_args(script))
    assert result.returncode == shrink.UNCHANGED_EXIT and "dibatalkan" in result.stderr


def test_final_grub_prefix_on_another_disk_without_partition_is_unknown():
    [ref] = _prefix_refs("(hd1)/grub", "x", 1)
    assert ref.kind == "partnum-other-disk" and ref.value == "hd1"


def test_final_identity_ignores_links_to_home_ancestors(tmp_path):
    home = tmp_path / "home"
    (home / ".wine" / "dosdevices").mkdir(parents=True)
    (home / ".bashrc").write_bytes(b"x")
    _symlink(tmp_path, home / ".wine" / "dosdevices" / "z")
    buffer = io.BytesIO()
    dotfiles.write_archive(buffer, home, [".bashrc"], exclude=[".wine"])
    assert ".bashrc" in _names(buffer)
