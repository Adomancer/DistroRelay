"""M3: hapus partisi Linux, pulihkan partisi, dan perluas C:. Laptop simulasi (FakeBcd), tanpa sistem asli."""

import argparse
import copy
import dataclasses
import json

import pytest

from distrorelay.windows import gpt_types
from distrorelay.windows.execute import ExecutionError, check_gate, execution_runner, partition_delete_script
from distrorelay.windows.runner import ReadOnlyViolation, powershell_args
from distrorelay.windows.uninstall_plan import partition_refusal
from distrorelay.windows.workflow import DATA_WORD, RestoreFlow
from helpers import P5_GUID, laptop_inventory
from test_execute import INSTALLATIONS
from test_uninstall_plan import plan_for
from test_workflow import make_flow, ready_for_gate

TYPED_P5 = {"id:disk0-p5": "disk0-p5"}


def remove_recovery(disks_json: dict) -> None:
    """Layout tanpa Recovery: partisi Linux tepat sesudah C: (umum di laptop yang Linux-nya dipasang belakangan)."""
    disks_json["Partitions"] = [p for p in disks_json["Partitions"] if p["PartitionNumber"] != 4]


# --- Rencana ---------------------------------------------------------------------


def test_partition_refusals():
    inventory = laptop_inventory()
    p5 = next(p for p in inventory.partitions if p.id == "disk0-p5")
    assert partition_refusal(p5, inventory) is None
    assert "drive letter" in partition_refusal(dataclasses.replace(p5, drive_letter="L"), inventory)
    assert "boot/system" in partition_refusal(dataclasses.replace(p5, is_system=True), inventory)
    assert "bukan tipe partisi Linux" in partition_refusal(
        dataclasses.replace(p5, category="basic", type_name="Basic data"), inventory
    )
    assert "GUID" in partition_refusal(dataclasses.replace(p5, guid=None), inventory)
    assert "tidak ditemukan" in partition_refusal(None, inventory)


def test_windows_partitions_stay_blocked(tmp_path):
    plan, _ = plan_for(tmp_path, "disk0-p3", "disk0-p4", "disk0-p1")
    assert plan.actions == [] and len(plan.blocked) == 3


def test_partition_action_carries_everything_needed_to_re_identify_it(tmp_path):
    plan, _ = plan_for(tmp_path, "disk0-p5")
    [action] = [a for a in plan.actions if a.kind == "delete-partition"]
    assert action.data["guid"] == P5_GUID and action.data["offset"] == 509961306112
    assert action.data["gpt_type"] == gpt_types.XBOOTLDR and action.data["role"] == "boot"
    assert action.data["needs_data_statement"] is False  # /boot: cukup backup metadata GPT
    assert any("GUID partisi" in d for d in action.details) and any("Wajib ketik ID partisi" in d for d in action.details)


def test_extend_c_only_when_the_freed_space_follows_c(tmp_path):
    inventory = laptop_inventory()
    inventory.partitions = [p for p in inventory.partitions if p.number != 4]

    plan, _ = plan_for(tmp_path, "disk0-p5", inventory=inventory)
    assert plan.extend_c_possible and "bersebelahan dengan C:" in plan.notes[0]
    assert not any(a.kind == "extend-c" for a in plan.actions)  # tidak pernah otomatis

    plan, _ = plan_for(tmp_path, "disk0-p5", inventory=inventory, extend_c=True)
    assert [a.kind for a in plan.actions][-2:] == ["delete-partition", "extend-c"]
    assert any("tidak bisa dipulihkan" in w for w in plan.warnings)

    plan, _ = plan_for(tmp_path, "disk0-p5", extend_c=True)  # laptop asli: ada Recovery di antaranya
    assert any(b.startswith("extend:c:") and "tidak bersebelahan" in b for b in plan.blocked)
    plan, _ = plan_for(tmp_path, "EFI\\fedora", extend_c=True)  # tanpa hapus partisi
    assert any(b.startswith("extend:c:") for b in plan.blocked)


# --- Runner ----------------------------------------------------------------------


def test_only_the_planned_partition_script_may_run(tmp_path):
    flow, bcd, ext = make_flow(tmp_path)
    flow.scan()
    plan = flow.select(INSTALLATIONS)
    runner = execution_runner(bcd, plan)
    [action] = [a for a in plan.actions if a.kind == "delete-partition"]
    other = dict(action.data, offset=action.data["offset"] + 4096)
    with pytest.raises(ReadOnlyViolation):
        runner.run(powershell_args(partition_delete_script(other)))
    with pytest.raises(ReadOnlyViolation):
        runner.run(powershell_args("# distrorelay:x\nRemove-Partition -DiskNumber 0 -PartitionNumber 3 -Confirm:$false"))
    assert bcd.partition(P5_GUID) is not None
    assert runner.run(powershell_args(partition_delete_script(action.data))).ok
    assert bcd.partition(P5_GUID) is None


def test_script_rejects_bad_guids():
    with pytest.raises(ExecutionError):
        partition_delete_script({"guid": "x'; Remove-Partition", "disk_guid": P5_GUID, "gpt_type": P5_GUID,
                                 "offset": 1, "size": 1})


# --- Alur lengkap ------------------------------------------------------------------


def uninstall_everything(tmp_path, *, setup=None, extend_c=False, flush_volume=None):
    flow, bcd, ext = make_flow(tmp_path, flush_volume=flush_volume)
    if setup:
        setup(bcd)
    assert flow.set_mode("vm", {}) == []
    flow.scan()
    flow.select(INSTALLATIONS, extend_c=extend_c)
    flow.run_preflight()
    flow.confirm("emergency-usb", True)
    assert flow.make_backup(ext).ok
    gate = flow.gate()
    assert gate.ok, gate.problems
    return flow, bcd


def test_full_uninstall_deletes_the_partition_last_and_post_check_is_green(tmp_path):
    flow, bcd = uninstall_everything(tmp_path)
    assert [t.key for t in flow.required_typing()] == ["id:disk0-p5"]
    assert flow.authorize("HAPUS") is None  # ID partisi belum diketik
    assert flow.authorize("HAPUS", {"id:disk0-p5": "disk0-p4"}) is None
    assert flow.typing_problems({}) == ["ID partisi disk0-p5: ketik disk0-p5 persis"]
    others = [dict(p) for p in bcd.disks["Partitions"] if p["Guid"].strip("{}") != P5_GUID]

    result = flow.execute(flow.authorize("HAPUS", TYPED_P5))
    assert result.completed, result.stopped
    assert result.steps[-1].kind == "delete-partition"
    assert bcd.partition(P5_GUID) is None and bcd.disks["Partitions"] == others
    checks = {c.name: c for c in flow.post_check()}
    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]
    assert {"Partisi yang dihapus sudah tidak ada", "Layout disk lain tidak berubah"} <= set(checks)


def test_partition_with_data_needs_the_backup_statement(tmp_path):
    def as_root(bcd):  # partisi yang sama, tapi bertipe root: isinya bisa data pribadi
        bcd.partition(P5_GUID)["GptType"] = "{" + gpt_types.LINUX_ROOT_X86_64 + "}"

    flow, bcd = uninstall_everything(tmp_path, setup=as_root)
    assert [t.key for t in flow.required_typing()] == ["id:disk0-p5", "data:disk0-p5"]
    assert flow.authorize("HAPUS", TYPED_P5) is None
    assert flow.authorize("HAPUS", dict(TYPED_P5, **{"data:disk0-p5": "data sudah dibackup"})) is None
    assert flow.authorize("HAPUS", dict(TYPED_P5, **{"data:disk0-p5": DATA_WORD})) is not None


def test_layout_change_mid_run_stops_before_the_partition(tmp_path):
    bcd_ref = {}

    def flush_volume(root):  # terjadi di langkah ESP, sebelum langkah partisi: mis. USB dicolok
        disks = bcd_ref["bcd"].disks
        if not any(p["PartitionNumber"] == 9 for p in disks["Partitions"]):
            extra = copy.deepcopy(disks["Partitions"][-1])
            extra.update(PartitionNumber=9, Guid="{99999999-0000-4000-8000-000000000009}", Offset=1, Size=1)
            disks["Partitions"].append(extra)

    flow, bcd = uninstall_everything(tmp_path, setup=lambda b: bcd_ref.update(bcd=b), flush_volume=flush_volume)
    result = flow.execute(flow.authorize("HAPUS", TYPED_P5))
    assert not result.completed and result.steps[-1].kind == "delete-partition" and not result.steps[-1].ok
    assert "Layout disk sebelum partisi dihapus" in result.steps[-1].message
    assert bcd.partition(P5_GUID) is not None


def test_remove_partition_failure_stops(tmp_path):
    flow, bcd = uninstall_everything(tmp_path)
    bcd.failures["ps:delete-partition"] = "Access denied"
    result = flow.execute(flow.authorize("HAPUS", TYPED_P5))
    assert not result.completed and "Remove-Partition gagal" in result.stopped
    assert bcd.partition(P5_GUID) is not None


def test_gate_needs_the_partition_in_the_backup(tmp_path):
    flow, bcd = uninstall_everything(tmp_path)
    plan = copy.deepcopy(flow.plan)
    action = next(a for a in plan.actions if a.kind == "delete-partition")
    action.data["size"] += 4096  # tidak cocok dengan partitions.json di backup
    gate = check_gate(plan, flow.report, flow.preflight, backup_id=flow.backup_id,
                      local_root=flow.deps.backups_root, external_roots=flow.deps.external_roots(flow.report),
                      confirmed=flow.confirmed)
    assert any("Metadata partisi disk0-p5" in p for p in gate.problems)


def test_backup_records_filesystem_uuids(tmp_path):
    flow, bcd = uninstall_everything(tmp_path)
    fsinfo = json.loads((flow.deps.backups_root / flow.backup_id / "fsinfo.json").read_text(encoding="utf-8"))
    assert fsinfo["disk0-p5"]["fs_type"] == "ext4" and fsinfo["disk0-p5"]["last_mounted"] == "/boot"


def test_extend_c_after_deleting_the_adjacent_partition(tmp_path):
    flow, bcd = uninstall_everything(tmp_path, setup=lambda b: remove_recovery(b.disks), extend_c=True)
    c_before = next(p for p in bcd.disks["Partitions"] if p["DriveLetter"] == "C")["Size"]
    result = flow.execute(flow.authorize("HAPUS", TYPED_P5))
    assert result.completed, result.stopped
    assert [s.kind for s in result.steps][-2:] == ["delete-partition", "extend-c"]
    assert next(p for p in bcd.disks["Partitions"] if p["DriveLetter"] == "C")["Size"] > c_before
    checks = {c.name: c for c in flow.post_check()}
    assert checks["C: diperluas"].ok and checks["Layout disk lain tidak berubah"].ok


# --- Pulihkan partisi ----------------------------------------------------------------


def uninstalled(tmp_path):
    flow, bcd = uninstall_everything(tmp_path)
    assert flow.execute(flow.authorize("HAPUS", TYPED_P5)).completed
    return flow, bcd


def test_restore_partition_with_the_same_offset_size_and_type(tmp_path):
    flow, bcd = uninstalled(tmp_path)
    restore = RestoreFlow(flow.deps)
    assert all(v.ok for v in restore.load(flow.backup_id))
    [choice] = restore.partition_choices()
    assert choice.key == P5_GUID and choice.blocked is None and "ext4" in choice.label

    assert restore.select([], [], ["disk0-p5"]) is None
    assert "disk0-p5" in restore.summary()
    results = restore.run(restore.authorize("PULIHKAN"))
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    assert any("UUID filesystem sama" in r.message for r in results)
    again = next(p for p in bcd.disks["Partitions"] if p["Offset"] == 509961306112)
    assert again["Size"] == 2147483648 and again["GptType"].strip("{}") == gpt_types.XBOOTLDR
    assert again["Guid"].strip("{}") != P5_GUID  # PARTUUID baru; isinya (UUID filesystem) tetap


def test_restore_partition_refused_when_space_is_used_or_it_still_exists(tmp_path):
    flow, bcd = uninstalled(tmp_path)
    squatter = copy.deepcopy(bcd.disks["Partitions"][-1])
    squatter.update(PartitionNumber=5, Guid="{55555555-0000-4000-8000-000000000005}", Offset=509961306112 + 2**20,
                    Size=2**20, GptType="{" + gpt_types.BASIC_DATA + "}", DriveLetter="D", Type="Basic")
    bcd.disks["Partitions"].append(squatter)
    restore = RestoreFlow(flow.deps)
    restore.load(flow.backup_id)
    [choice] = restore.partition_choices()
    assert "Ruangnya sudah dipakai disk0-p5" in choice.blocked
    assert "sudah dipakai" in restore.select([], [], [P5_GUID])

    flow2, bcd2 = uninstall_everything(tmp_path / "masih-ada")  # backup dibuat, partisi belum dihapus
    restore = RestoreFlow(flow2.deps)
    restore.load(flow2.backup_id)
    assert restore.partition_choices()[0].blocked == "Masih ada di disk, tidak perlu dipulihkan"


def test_cli_restore_partition(tmp_path, monkeypatch):
    from distrorelay.cli.uninstall import cmd_restore

    flow, bcd = uninstalled(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "PULIHKAN")
    args = argparse.Namespace(backup=flow.backup_id, esp_item=[], boot_entry=[], partition=["disk0-p5"])
    assert cmd_restore(args, flow=RestoreFlow(flow.deps)) == 0
    assert any(p["Offset"] == 509961306112 for p in bcd.disks["Partitions"])


def test_cli_uninstall_asks_for_the_partition_id(tmp_path, monkeypatch):
    from distrorelay.cli.uninstall import cmd_uninstall
    from distrorelay.windows.backup import backup_runner, create_backup

    flow, bcd, ext = make_flow(tmp_path)
    report = flow.scan()
    backup = create_backup(report, backup_runner(bcd, root=tmp_path / "local"), flow.deps.mount_esp,
                           local_root=tmp_path / "local", usb_root=ext)
    answers = iter(["vm", "ya", "disk0-p4"])  # ID salah -> berhenti sebelum HAPUS ditanyakan
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    args = argparse.Namespace(backup=backup.backup_id, select=INSTALLATIONS)
    assert cmd_uninstall(args, flow=flow) == 1
    assert bcd.partition(P5_GUID) is not None

    answers = iter(["vm", "ya", "disk0-p5", "HAPUS"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    flow2, bcd2, ext2 = make_flow(tmp_path / "kedua")
    report = flow2.scan()
    backup = create_backup(report, backup_runner(bcd2, root=tmp_path / "kedua" / "local"), flow2.deps.mount_esp,
                           local_root=tmp_path / "kedua" / "local", usb_root=ext2)
    args = argparse.Namespace(backup=backup.backup_id, select=INSTALLATIONS)
    assert cmd_uninstall(args, flow=flow2) == 0
    assert bcd2.partition(P5_GUID) is None
