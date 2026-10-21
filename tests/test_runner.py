import pytest

from distrohop.windows.bitlocker import BITLOCKER_SCRIPT
from distrohop.windows.disks import DISKS_SCRIPT
from distrohop.windows.runner import (
    FakeRunner,
    ReadOnlyRunner,
    ReadOnlyViolation,
    check_read_only,
    command_key,
    decode_powershell,
    powershell_args,
)


def test_powershell_args_round_trip():
    script = "# distrohop:contoh\nGet-Disk | ConvertTo-Json"
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
        powershell_args("# distrohop:jahat\nGet-Partition | Remove-Partition -Confirm:$false"),
        powershell_args("get-partition | remove-partition"),
        powershell_args("Suspend-BitLocker -MountPoint C:"),
        [],
    ],
)
def test_read_only_blocks_anything_that_can_change_the_system(args):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(args)


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
