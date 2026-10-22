import pytest

from distrorelay.windows.bitlocker import BITLOCKER_SCRIPT
from distrorelay.windows.disks import DISKS_SCRIPT
from distrorelay.windows.runner import (
    FakeRunner,
    ReadOnlyRunner,
    ReadOnlyViolation,
    check_read_only,
    command_key,
    decode_powershell,
    powershell_args,
)


def test_powershell_args_round_trip():
    script = "# distrorelay:contoh\nGet-Disk | ConvertTo-Json"
    args = powershell_args(script)
    assert decode_powershell(args) == script
    assert command_key(args) == "ps:contoh"


def test_command_key_for_plain_commands():
    assert command_key(["C:\\Windows\\System32\\bcdedit.exe", "/enum", "firmware", "/v"]) == "bcdedit /enum firmware /v"


@pytest.mark.parametrize(
    "args",
    [
        powershell_args(DISKS_SCRIPT),
        powershell_args(BITLOCKER_SCRIPT),
        ["bcdedit", "/enum", "firmware", "/v"],
        ["bcdedit.exe", "/enum", "all"],
        ["mountvol", "Z:", "/S"],
        ["mountvol", "z:", "/D"],
        ["manage-bde", "-status", "C:"],
    ],
)
def test_read_only_allows_scan_commands(args):
    check_read_only(args)


@pytest.mark.parametrize(
    "args",
    [
        ["bcdedit", "/delete", "{0b5f2d1e-7a39-11ef-9a6b-806e6f6e6963}"],
        ["bcdedit", "/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"],
        ["bcdedit", "/export", "C:\\bcd.bak"],
        ["bcdedit", "/enum", "firmware", "/v", "/delete"],
        ["mountvol", "Z:", "/P"],
        ["mountvol", "Z:", "\\\\?\\Volume{x}\\"],
        ["diskpart", "/s", "script.txt"],
        ["manage-bde", "-off", "C:"],
        powershell_args("# distrorelay:jahat\nGet-Partition | Remove-Partition -Confirm:$false"),
        powershell_args("get-partition | remove-partition"),
        powershell_args("Suspend-BitLocker -MountPoint C:"),
        [],
    ],
)
def test_read_only_blocks_anything_that_can_change_the_system(args):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(args)


def test_read_only_allows_reading_an_exported_store_and_signatures():
    check_read_only(["bcdedit", "/store", "C:\\backup\\bcd-store.bak", "/enum", "all"])
    from distrorelay.windows.signature import signature_script

    check_read_only(powershell_args(signature_script("Z:\\EFI\\Pop_OS-1234\\file-with-hyphen.efi")))


@pytest.mark.parametrize(
    "script",
    [
        "rm C:\\Windows\\x",
        "Get-Disk; del 'C:\\x'",
        "iex 'Remove-Partition'",
        "& 'Remove-Partition' -DiskNumber 0",
        "Mount-DiskImage -ImagePath 'x.iso'",  # tanpa -Access ReadOnly
    ],
)
def test_powershell_aliases_call_operator_and_writable_mounts_are_blocked(script):
    from distrorelay.windows.preflight import M2A_CMDLETS
    from distrorelay.windows.runner import ALLOWED_CMDLETS, check_powershell

    with pytest.raises(ReadOnlyViolation):
        check_powershell(script, ALLOWED_CMDLETS | M2A_CMDLETS)


def test_allow_list_runner_only_allows_export_into_backup_folder(tmp_path):
    from distrorelay.windows.backup import backup_runner

    fake = FakeRunner()
    fake.add("bcdedit /export *")
    runner = backup_runner(fake, root=tmp_path / "backups")
    runner.run(["bcdedit", "/export", str(tmp_path / "backups" / "x" / "bcd-store.bak")])
    for args in (
        ["bcdedit", "/export", str(tmp_path / "lain" / "bcd.bak")],
        ["bcdedit", "/export", str(tmp_path / "backups" / ".." / "keluar.bak")],
        ["bcdedit", "/delete", "{0b5f2d1e-7a39-11ef-9a6b-806e6f6e6963}"],
        ["bcdedit", "/set", "{fwbootmgr}", "displayorder", "{bootmgr}", "/addfirst"],
    ):
        with pytest.raises(ReadOnlyViolation):
            runner.run(args)
    assert len(fake.calls) == 1


def test_read_only_runner_blocks_before_running():
    fake = FakeRunner()
    runner = ReadOnlyRunner(fake)
    with pytest.raises(ReadOnlyViolation):
        runner.run(["bcdedit", "/delete", "{x}"])
    assert fake.calls == []


def test_fake_runner_records_and_answers():
    fake = FakeRunner()
    fake.add("bcdedit /enum firmware /v", stdout="ok")
    assert fake.run(["bcdedit", "/enum", "firmware", "/v"]).stdout == "ok"
    assert fake.run(["mountvol", "Z:", "/S"]).returncode == 127
    assert len(fake.calls) == 2
