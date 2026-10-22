"""Backup dari distro lama (M5): daftar paket, kurasi, dotfiles, file sensitif terenkripsi, dan pemakaiannya
di setup (Linux), `profile import` dan aturan backup bertingkat (Windows).

gpg, findmnt, dan lsblk lewat FakeRunner. "Enkripsi" palsu: header SKESK + XOR, cukup untuk memastikan isi asli
tidak pernah tertulis ke disk.
"""

import argparse
import json
import os
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from distrorelay.common import packages
from distrorelay.common import profile as profile_mod
from distrorelay.common.profile import Profile
from distrorelay.windows import kit_export
from distrorelay_linux import backup, cli, provision
from distrorelay_linux import kit as linux_kit
from distrorelay_linux.distro import detect
from distrorelay_linux.runner import FakeRunner
from helpers import LAPTOP_BOOT_UUID
from test_linux_kit import OS_RELEASE
from test_partition import TYPED_P5, uninstall_everything

FEDORA = detect(OS_RELEASE["fedora"])
NOW = datetime(2026, 10, 22, 15, 0, 0)
ROOT_UUID = "0F6D1C3E-9B2A-4C5D-8E7F-1A2B3C4D5E6F"
LUKS_UUID = "7c1e2d3f-4a5b-4c6d-9e8f-0a1b2c3d4e5f"
SECRET = b"-----BEGIN OPENSSH PRIVATE KEY-----\nrahasia\n"
TOKEN = b"SECRET-TOKEN\n"

PROFILE = Profile(
    name="uji",
    native=["git"],
    dotfiles=["~/.bashrc", "~/.config/app"],
    sensitive=["~/.ssh", "~/.config/app/token"],
    exclude=["vlc"],
)


def fake_encrypt(cmd, data):
    return 0, b"\x8c\x0d" + bytes(b ^ 0x5A for b in data)


def fake_decrypt(cmd, data):
    return 0, bytes(b ^ 0x5A for b in Path(cmd.args[-1]).read_bytes()[2:])


def make_home(root: Path) -> Path:
    files = {
        ".bashrc": b"alias ll='ls -l'\n",
        ".config/app/settings.ini": b"theme=dark\n",
        ".config/app/token": TOKEN,
        ".ssh/id_ed25519": SECRET,
        ".ssh/id_ed25519.pub": b"ssh-ed25519 AAAA uji\n",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def make_runner(*, usb: bool = True, gpg: bool = True) -> FakeRunner:
    runner = FakeRunner(programs={"flatpak", "gpg"} if gpg else {"flatpak"})
    runner.add("dnf repoquery", 0, "kernel-core\ngit\nfirefox\nvlc\nfedora-workstation-repositories\nmy-tool\ngit\n")
    runner.add("flatpak list", 0, "com.spotify.Client\n")
    source = "/dev/sdb1" if usb else "/dev/nvme0n1p6"
    runner.add("findmnt -J -o SOURCE --target", 0, json.dumps({"filesystems": [{"source": source}]}))
    runner.add(f"lsblk -J -s -o NAME,TRAN {source}", 0, json.dumps(
        {"blockdevices": [{"name": source[5:], "tran": None,
                           "children": [{"name": source[5:8], "tran": "usb" if usb else "nvme"}]}]}))
    for target in ("/", "/home"):
        runner.add(f"findmnt -J -o SOURCE,UUID,FSTYPE,TARGET --target {target}", 0, json.dumps(
            {"filesystems": [{"source": f"/dev/mapper/luks-7c1e[/{'root' if target == '/' else 'home'}]",
                              "uuid": ROOT_UUID, "fstype": "btrfs", "target": target}]}))
    runner.add("lsblk -J -s -o NAME,UUID,FSTYPE /dev/mapper/luks-7c1e", 0, json.dumps(
        {"blockdevices": [{"name": "luks-7c1e", "uuid": ROOT_UUID, "fstype": "btrfs",
                           "children": [{"name": "nvme0n1p6", "uuid": LUKS_UUID, "fstype": "crypto_LUKS"}]}]}))
    runner.byte_handlers.update({"gpg --symmetric": fake_encrypt, "gpg --decrypt": fake_decrypt})
    return runner


def make_kit(root: Path, profile: Profile = PROFILE):
    result = kit_export.export_kit(profile, packages.builtin(), root, now=NOW)
    assert result.ok, result.problems
    return linux_kit.load(result.kit_dir)


def run_backup(tmp_path, *, runner=None, curate=lambda candidates: ["firefox"], **kwargs):
    kit = make_kit(tmp_path / "usb")
    home = make_home(tmp_path / "home")
    result = backup.create_backup(kit, runner or make_runner(), FEDORA, home=home, curate=curate, now=NOW, **kwargs)
    return kit, result


def files_containing(root: Path, needle: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


# --- Daftar paket & kurasi -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, responses, native, aur",
    [
        ("fedora", {"dnf repoquery --userinstalled": "git\nfirefox\n"}, ["git", "firefox"], []),
        ("arch", {"pacman -Qqe": "base\ngit\nparu-bin\n", "pacman -Qqem": "paru-bin\n"}, ["base", "git"], ["paru-bin"]),
        ("ubuntu", {"apt-mark showmanual": "git\n\nvlc\n"}, ["git", "vlc"], []),
    ],
)
def test_installed_packages_per_family(name, responses, native, aur):
    runner = FakeRunner()
    for prefix, stdout in responses.items():
        runner.add(prefix, 0, stdout)
    found = backup.installed_packages(runner, detect(OS_RELEASE[name]))
    assert (found.native, found.aur, found.flatpak, found.problems) == (native, aur, [], [])


def test_failed_package_query_is_a_problem_not_a_crash():
    runner = FakeRunner(programs={"flatpak"})
    runner.add("dnf repoquery", 1, "")
    found = backup.installed_packages(runner, FEDORA)
    assert found.native == [] and len(found.problems) == 1 and "dnf repoquery" in found.problems[0]


def test_base_packages_and_previous_curation_are_filtered():
    patterns = backup.base_patterns("fedora")
    names = ["kernel-core", "git", "fedora-release", "NetworkManager-wifi", "vlc", "steam"]
    kept, removed = backup.filter_packages(names, patterns, exclude=["steam"])
    assert kept == ["git", "vlc"] and removed == ["kernel-core", "fedora-release", "NetworkManager-wifi", "steam"]
    assert backup.base_patterns("arch") and backup.base_patterns("debian")
    assert backup.base_patterns("tidak-ada") == []


def test_parse_selection():
    assert backup.parse_selection("3 5 8-10", 10) == {3, 5, 8, 9, 10}
    assert backup.parse_selection("1,2", 3) == {1, 2}
    assert backup.parse_selection("  ", 3) == set()
    for bad in ("0", "4", "a", "2-1", "1-", "-1", "1-4"):
        assert backup.parse_selection(bad, 3) is None, bad


def test_filesystems_record_luks_and_strip_btrfs_subvolumes():
    found = backup.filesystems(make_runner())
    assert found["/"] == {"mountpoint": "/", "uuid": ROOT_UUID, "fstype": "btrfs",
                          "source": "/dev/mapper/luks-7c1e", "luks_uuid": LUKS_UUID}
    assert backup.covered_uuids({"filesystems": found}) == {ROOT_UUID.lower(), LUKS_UUID}
    assert backup.filesystems(FakeRunner()) == {}  # findmnt gagal / output kosong


def test_usb_detection_follows_the_device_tree():
    assert backup.on_usb(make_runner(usb=True), Path("/run/media/u/USB"))
    assert not backup.on_usb(make_runner(usb=False), Path("/home/u"))
    assert not backup.on_usb(FakeRunner(), Path("/x"))


# --- Membuat backup ---------------------------------------------------------------------------


def test_backup_on_usb_with_encrypted_sensitive_files(tmp_path):
    seen = []
    kit, result = run_backup(tmp_path, curate=lambda candidates: seen.append(candidates) or ["firefox"])
    assert seen == [["git", "firefox", "my-tool"]]  # bawaan distro dan exclude lama (vlc) sudah disaring
    assert result.path == kit.backups_dir / "20261022-150000-fedora"
    assert [p.name for p in kit.backups_dir.iterdir()] == [result.path.name]  # tidak ada sisa staging
    manifest = result.manifest
    assert manifest["profile_update"] == {"native": ["git"], "distro_specific": {"fedora": ["my-tool"]}, "aur": [],
                                          "flatpak": ["com.spotify.Client"], "exclude": ["firefox"]}
    assert "firefox" in manifest["packages"]["not_carried"] and "kernel-core" in manifest["packages"]["not_carried"]
    assert manifest["filesystems"]["/home"]["uuid"] == ROOT_UUID and "Bukan backup dokumen" in manifest["warning"]
    assert backup.verify(result.path) == []

    with tarfile.open(result.path / backup.DOTFILES) as tar:
        names = set(tar.getnames())
    assert names == {".bashrc", ".config/app", ".config/app/settings.ini"}  # token sensitif tidak ikut

    encrypted = (result.path / backup.SENSITIVE).read_bytes()
    assert encrypted[:1] == b"\x8c" and manifest["sensitive"]["location"] == "here"
    assert manifest["sensitive"]["count"] == 3
    for needle in (SECRET, TOKEN):
        assert files_containing(tmp_path / "usb", needle) == []  # isi asli tidak pernah ditulis ke drive


def test_sensitive_files_are_refused_off_usb(tmp_path):
    kit, result = run_backup(tmp_path, runner=make_runner(usb=False))
    assert result.manifest["sensitive"] is None and not (result.path / backup.SENSITIVE).exists()
    assert any("tidak di USB" in note for note in result.notes)
    assert backup.verify(result.path) == []
    for needle in (SECRET, TOKEN):
        assert files_containing(tmp_path / "usb", needle) == []


def test_sensitive_files_go_to_another_kit_on_usb(tmp_path):
    other = tmp_path / "usb2" / "DistroRelay" / "linux-backups"
    kit, result = run_backup(tmp_path, runner=make_runner(usb=False), usb_backup_dirs=lambda: [other])
    copy = other / result.path.name / backup.SENSITIVE
    assert copy.is_file() and result.manifest["sensitive"]["location"] == str(other / result.path.name)
    assert backup.sensitive_path(result.path, result.manifest, []) is None
    assert backup.sensitive_path(result.path, result.manifest, [other]) == copy
    assert backup.list_backups(other) == []  # salinan sensitif bukan backup tersendiri
    copy.write_bytes(copy.read_bytes() + b"x")
    assert backup.sensitive_path(result.path, result.manifest, [other]) is None


@pytest.mark.parametrize("gpg, handler, expected", [
    (False, None, "gpg tidak ada"),
    (True, lambda cmd, data: (2, b""), "gpg gagal"),
    (True, lambda cmd, data: (0, data), "gpg gagal"),  # output bukan OpenPGP terenkripsi
])
def test_gpg_problems_skip_only_the_sensitive_part(tmp_path, gpg, handler, expected):
    runner = make_runner(gpg=gpg)
    if handler:
        runner.byte_handlers["gpg --symmetric"] = handler
    kit, result = run_backup(tmp_path, runner=runner)
    assert result.manifest["sensitive"] is None and backup.verify(result.path) == []
    assert any(expected in note for note in result.notes)
    assert files_containing(tmp_path / "usb", SECRET) == []


def test_no_sensitive_flag_and_unsupported_distro(tmp_path):
    runner = make_runner()
    kit, result = run_backup(tmp_path, runner=runner, include_sensitive=False)
    assert result.manifest["sensitive"] is None and not any(c.args[0] == "gpg" for c in runner.calls)
    with pytest.raises(backup.BackupError):
        backup.create_backup(kit, make_runner(), detect(OS_RELEASE["opensuse"]), home=tmp_path, curate=list)


def test_verify_list_and_latest(tmp_path):
    kit, older = run_backup(tmp_path)
    newer = backup.create_backup(kit, make_runner(), FEDORA, home=tmp_path / "home", curate=lambda c: [],
                                 now=datetime(2026, 10, 23, 9, 0, 0))
    assert backup.latest_backup(kit.backups_dir) == newer.path
    (newer.path / backup.DOTFILES).write_bytes(b"rusak")
    listed = backup.list_backups(kit.backups_dir)
    assert [(f.name, bool(p)) for f, _, p in listed] == [(newer.path.name, True), (older.path.name, False)]
    assert "hash tidak cocok" in listed[0][2][0]
    assert backup.latest_backup(kit.backups_dir) == older.path
    (older.path / backup.MANIFEST).write_text("{}")
    assert backup.latest_backup(kit.backups_dir) is None
    assert backup.list_backups(tmp_path / "tidak-ada") == []


# --- Setup di distro baru memakai backup ---------------------------------------------------


def test_setup_restores_dotfiles_and_private_sensitive_files(tmp_path):
    kit, result = run_backup(tmp_path)
    runner = make_runner()
    new_home = tmp_path / "rumah-baru"
    plan = provision.build_plan(
        kit.profile, kit.packages, FEDORA, is_root=False, has_paru=False, home=new_home, stamp="s",
        workdir=str(tmp_path), backup_dir=result.path,
        sensitive_file=backup.sensitive_path(result.path, result.manifest, []),
        decrypt=lambda path: backup.decrypt(runner, path),
    )
    assert [s.key for s in plan.steps][-2:] == ["dotfiles", "sensitive"]
    results = provision.execute(plan, runner, approve=lambda q: True)
    assert all(r.status == "ok" for r in results), results
    assert (new_home / ".bashrc").read_bytes() == b"alias ll='ls -l'\n"
    assert (new_home / ".ssh" / "id_ed25519").read_bytes() == SECRET
    assert (new_home / ".config" / "app" / "token").read_bytes() == TOKEN
    if os.name == "posix":
        assert (new_home / ".ssh").stat().st_mode & 0o777 == 0o700
        assert (new_home / ".ssh" / "id_ed25519").stat().st_mode & 0o777 == 0o600
        assert (new_home / ".ssh" / "id_ed25519.pub").stat().st_mode & 0o777 == 0o644


def test_setup_notes_missing_sensitive_copy(tmp_path):
    kit, result = run_backup(tmp_path, runner=make_runner(usb=False))
    plan = provision.build_plan(
        kit.profile, kit.packages, FEDORA, is_root=False, has_paru=False, home=tmp_path / "baru", stamp="s",
        workdir=str(tmp_path), backup_dir=result.path, sensitive_file=None, decrypt=None,
    )
    assert "sensitive" not in [s.key for s in plan.steps]
    assert any("File sensitif tidak dipulihkan" in note for note in plan.notes)


def test_wrong_passphrase_fails_only_that_step(tmp_path):
    kit, result = run_backup(tmp_path)
    runner = make_runner()
    runner.byte_handlers["gpg --decrypt"] = lambda cmd, data: (2, b"")
    plan = provision.build_plan(
        kit.profile, kit.packages, FEDORA, is_root=False, has_paru=False, home=tmp_path / "baru", stamp="s",
        workdir=str(tmp_path), backup_dir=result.path, sensitive_file=result.path / backup.SENSITIVE,
        decrypt=lambda path: backup.decrypt(runner, path),
    )
    statuses = {r.key: r.status for r in provision.execute(plan, runner, approve=lambda q: True)}
    assert statuses["sensitive"] == "gagal" and statuses["dotfiles"] == "ok"
    assert not (tmp_path / "baru" / ".ssh").exists()


def test_linux_cli_backup_backups_and_setup(tmp_path, monkeypatch, capsys):
    kit = make_kit(tmp_path / "usb")
    home = make_home(tmp_path / "home")
    runner = make_runner()
    monkeypatch.setattr("distrorelay_linux.distro.read", lambda: FEDORA)
    monkeypatch.setattr("distrorelay_linux.cli.SubprocessRunner", lambda: runner)
    monkeypatch.setattr("distrorelay_linux.cli.Path.home", lambda: home)
    monkeypatch.setattr("distrorelay_linux.cli.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("distrorelay_linux.kit.default_search_roots", lambda user=None: [])
    root = ["--kit", str(kit.root)]
    assert cli.main(root + ["backup", "--yes", "--no-sensitive"]) == 0
    out = capsys.readouterr().out
    assert "Backup tersimpan" in out and "BUKAN backup dokumen" in out and "profile import" in out
    assert cli.main(root + ["backups"]) == 0
    assert "[ok]" in capsys.readouterr().out
    assert cli.main(root + ["setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Pulihkan dotfiles dari" in out and "File sensitif tidak dipulihkan" in out
    assert cli.main(root + ["setup", "--dry-run", "--backup", str(tmp_path / "tidak-ada")]) == 1


def test_kit_carries_the_base_package_lists(tmp_path):
    kit = make_kit(tmp_path)
    relative = backup._BASE_DIR.relative_to(Path(backup.__file__).resolve().parent.parent)
    assert backup.base_patterns("fedora", kit.root / relative) == backup.base_patterns("fedora")


# --- Windows: profile import ------------------------------------------------------------------


def test_merge_update_only_adds_and_respects_exclude():
    profile = Profile(native=["git", "firefox"], exclude=["vlc"], distro_specific={"arch": ["yay"]})
    update = {"native": ["git", "vlc", "htop", "firefox"], "distro_specific": {"fedora": ["my-tool", "vlc"]},
              "aur": [], "flatpak": ["com.spotify.Client"], "exclude": ["firefox", "steam"]}
    merged, changes = profile_mod.merge_update(profile, update)
    assert merged.native == ["git", "firefox", "htop"]  # vlc tetap tidak dibawa, firefox yang ditulis manual tetap
    assert merged.distro_specific == {"arch": ["yay"], "fedora": ["my-tool"]}
    assert merged.exclude == ["vlc", "firefox", "steam"] and merged.flatpak == ["com.spotify.Client"]
    assert any("firefox" in c and "manual" in c for c in changes)
    again, more = profile_mod.merge_update(merged, update)
    assert again == merged and more == []


def test_windows_profile_import(tmp_path, monkeypatch, capsys):
    from distrorelay.__main__ import _run_profile_command

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    kit, result = run_backup(tmp_path)
    assert _run_profile_command(argparse.Namespace(profile_command="init", force=False)) == 0
    path = kit_export.profile_path()
    before = path.read_text(encoding="utf-8")

    def run(directory, yes=True):
        return _run_profile_command(argparse.Namespace(profile_command="import", directory=str(directory), yes=yes))

    monkeypatch.setattr("distrorelay.__main__._ask", lambda prompt: "tidak")
    assert run(result.path, yes=False) == 1 and path.read_text(encoding="utf-8") == before
    assert run(result.path) == 0
    profile = kit_export.load_profile(path)
    assert profile.distro_specific["fedora"] == ["my-tool"] and "firefox" in profile.exclude
    assert "com.spotify.Client" in profile.flatpak
    assert [p.read_text(encoding="utf-8") for p in path.parent.glob("profile.toml.bak-*")] == [before]
    capsys.readouterr()
    assert run(result.path) == 0 and "tidak ada yang berubah" in capsys.readouterr().out

    (result.path / backup.DOTFILES).write_bytes(b"rusak")
    assert run(result.path) == 1 and "hash tidak cocok" in capsys.readouterr().out


# --- Windows: aturan backup bertingkat (a) -----------------------------------------------------


def linux_backup_on(ext: Path, fs_uuid: str, *, broken: bool = False) -> None:
    folder = ext / "DistroRelay" / "linux-backups" / "20261022-150000-fedora"
    folder.mkdir(parents=True)
    manifest = {"format": backup.BACKUP_FORMAT, "backup_id": folder.name,
                "filesystems": {"/": {"uuid": fs_uuid.upper(), "luks_uuid": None}},
                "files": [{"path": backup.DOTFILES, "sha256": "0" * 64}] if broken else []}
    (folder / backup.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def root_partition_with_linux_backup(tmp_path, **kwargs):
    from distrorelay.windows import gpt_types
    from helpers import P5_GUID

    def setup(bcd):
        bcd.partition(P5_GUID)["GptType"] = "{" + gpt_types.LINUX_ROOT_X86_64 + "}"
        linux_backup_on(tmp_path / "ext", LAPTOP_BOOT_UUID, **kwargs)

    return uninstall_everything(tmp_path, setup=setup)


def test_verified_linux_backup_does_not_replace_the_data_statement(tmp_path):
    """Keputusan 2026-10-22 (review): backup Linux = paket + dotfiles, bukan data, dan root sering memuat /home.
    Keberadaannya hanya ditampilkan; pernyataan DATA SUDAH DIBACKUP tetap wajib."""
    flow, bcd = root_partition_with_linux_backup(tmp_path)
    assert [t.key for t in flow.required_typing()] == ["id:disk0-p5", "data:disk0-p5"]
    assert any("sudah dibackup (20261022-150000-fedora" in w and "tetap diminta" in w for w in flow.plan.warnings)
    assert flow.authorize("HAPUS", TYPED_P5) is None
    assert flow.authorize("HAPUS", {**TYPED_P5, "data:disk0-p5": "DATA SUDAH DIBACKUP"}) is not None


def test_broken_linux_backup_does_not_count(tmp_path):
    flow, bcd = root_partition_with_linux_backup(tmp_path, broken=True)
    assert [t.key for t in flow.required_typing()] == ["id:disk0-p5", "data:disk0-p5"]
    assert flow.authorize("HAPUS", TYPED_P5) is None
