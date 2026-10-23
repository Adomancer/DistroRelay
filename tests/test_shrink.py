"""Shrink C: (M6): rencana, script persis, journal, post-check, dan CLI-nya."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from distrorelay.windows import shrink
from distrorelay.windows.journal import sessions
from distrorelay.windows.runner import (
    ALLOWED_CMDLETS,
    FakeRunner,
    ReadOnlyViolation,
    check_powershell,
    decode_powershell,
    powershell_args,
)

GIB = shrink.GIB
C_GUID = "{c3b2a1d0-1111-4222-8333-444455556666}"
BEFORE = {
    "DiskNumber": 0, "PartitionNumber": 3, "DiskGuid": "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}", "Guid": C_GUID,
    "Offset": 290455552, "Size": 300 * GIB, "SizeMin": 120 * GIB, "SizeMax": 300 * GIB, "Free": 150 * GIB,
    "FileSystem": "NTFS",
}


def drive(**changes) -> shrink.CDrive:
    return shrink.parse_query(json.dumps({**BEFORE, **changes}))


def test_query_script_is_read_only():
    check_powershell(shrink.QUERY_SCRIPT, ALLOWED_CMDLETS)


def test_parse_query_and_limits():
    d = drive()
    assert (d.disk_number, d.partition_number, d.guid, d.size) == (0, 3, C_GUID, 300 * GIB)
    assert d.max_shrink == 130 * GIB  # dibatasi sisa kosong 20 GB, bukan batas Windows 180 GB
    assert drive(SizeMin=250 * GIB).max_shrink == 50 * GIB
    with pytest.raises(shrink.ShrinkError, match="GPT"):
        drive(Guid="")
    with pytest.raises(shrink.ShrinkError, match="tidak terbaca"):
        shrink.parse_query("bukan json")


@pytest.mark.parametrize(
    "changes, size, problem",
    [
        ({}, 0.5 * GIB, "minimal"),
        ({"SizeMin": 280 * GIB}, 30 * GIB, "Windows hanya mengizinkan"),
        ({"Free": 40 * GIB}, 30 * GIB, "sisa ruang kosong"),
        ({"FileSystem": "ReFS"}, 30 * GIB, "bukan NTFS"),
    ],
)
def test_plan_rejections(changes, size, problem):
    plan = shrink.plan_shrink(drive(**changes), int(size))
    assert not plan.ok and any(problem in p for p in plan.problems), plan.problems


def test_plan_rounds_up_to_mib_and_builds_exact_script():
    plan = shrink.plan_shrink(drive(), 60 * GIB + 1)
    assert plan.ok and plan.shrink_by == 60 * GIB + shrink.MIB
    assert plan.new_size == 300 * GIB - plan.shrink_by
    script = plan.script
    assert f"'{C_GUID}'" in script
    assert f"Resize-Partition -DiskNumber 0 -PartitionNumber 3 -Size {plan.new_size}" in script
    assert f"$p.Size -ne {300 * GIB}" in script and "$p.Offset -ne 290455552" in script


def make_runner(after_size=None, fail=False):
    runner = FakeRunner()
    runner.add("ps:shrink-query", json.dumps(BEFORE))

    def resized(args):
        runner.add("ps:shrink-after", json.dumps({key: BEFORE[key] for key in ("DiskNumber", "Guid", "Offset")}
                                                 | {"Size": after_size}))

    runner.add("ps:shrink-c", "{}", returncode=1 if fail else 0, stderr="Resize gagal" if fail else "",
               effect=None if fail else resized)
    return runner


def test_execute_runs_only_the_planned_script_and_post_checks(tmp_path):
    plan = shrink.plan_shrink(drive(), 60 * GIB)
    runner = make_runner(after_size=plan.new_size)
    result = shrink.execute(plan, runner, journal_root=tmp_path)
    assert result.ok and result.after.size == plan.new_size
    scripts = [decode_powershell(c) for c in runner.calls]
    assert scripts.count(plan.script) == 1
    # P-3: post-check tidak mengulang Get-PartitionSupportedSize (query info bisa ±1 menit).
    assert scripts.count(shrink.QUERY_SCRIPT) == 0 and scripts.count(shrink.AFTER_SCRIPT) == 1
    [session] = sessions(tmp_path)
    assert session.finished and session.ok and session.post_check_ok and not session.needs_attention


def test_execute_failure_and_post_check_mismatch_are_journaled(tmp_path):
    plan = shrink.plan_shrink(drive(), 60 * GIB)
    failed = shrink.execute(plan, make_runner(fail=True), journal_root=tmp_path / "a")
    assert not failed.ok and "Resize gagal" in failed.message
    assert sessions(tmp_path / "a")[0].needs_attention
    mismatch = shrink.execute(plan, make_runner(after_size=plan.new_size + GIB), journal_root=tmp_path / "b")
    assert not mismatch.ok and "Post-check" in mismatch.message
    assert sessions(tmp_path / "b")[0].post_check_ok is False


def test_other_resize_scripts_are_refused(tmp_path):
    plan = shrink.plan_shrink(drive(), 60 * GIB)
    from distrorelay.windows.runner import AllowListRunner

    runner = AllowListRunner(FakeRunner(), scripts=frozenset({plan.script}))
    other = shrink.plan_shrink(drive(), 100 * GIB).script
    with pytest.raises(ReadOnlyViolation):
        runner.run(powershell_args(other))
    with pytest.raises(shrink.ShrinkError):
        shrink.execute(shrink.plan_shrink(drive(Free=30 * GIB), 60 * GIB), FakeRunner(), journal_root=tmp_path)


# --- CLI -----------------------------------------------------------------------------------------


def run_cli(monkeypatch, tmp_path, size, answers):
    from distrorelay import __main__ as cli

    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    plan_size = shrink.plan_shrink(drive(), int(size * GIB)).new_size if size else None
    runner = make_runner(after_size=plan_size)
    code = cli._run_shrink_command(Namespace(size=size), inner=runner, journal_root=tmp_path)
    return code, runner


def test_cli_info_only_changes_nothing(monkeypatch, tmp_path, capsys):
    code, runner = run_cli(monkeypatch, tmp_path, None, [])
    assert code == 0 and "maksimal 130 GB" in capsys.readouterr().out
    assert all("shrink-c" not in (decode_powershell(c) or "")[:30] for c in runner.calls)


def test_cli_wrong_word_cancels(monkeypatch, tmp_path, capsys):
    code, runner = run_cli(monkeypatch, tmp_path, 60, ["kecilkan"])
    assert code == 1 and "Dibatalkan" in capsys.readouterr().out
    assert not any((decode_powershell(c) or "").startswith("# distrorelay:shrink-c") for c in runner.calls)


def test_cli_shrinks_after_typed_word(monkeypatch, tmp_path, capsys):
    code, runner = run_cli(monkeypatch, tmp_path, 60, ["KECILKAN"])
    assert code == 0 and "C: sekarang 240 GB" in capsys.readouterr().out
    assert sessions(tmp_path)[0].ok


def test_cli_rejects_too_large_shrink(monkeypatch, tmp_path, capsys):
    code, _ = run_cli(monkeypatch, tmp_path, 200, [])
    assert code == 1 and "[GAGAL]" in capsys.readouterr().out
