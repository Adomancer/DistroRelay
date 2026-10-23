"""Linux kit (M4): deteksi distro, perintah package manager, rencana & eksekusi setup, dotfiles, export kit.

Semua tanpa Linux sungguhan: perintah lewat FakeRunner, file di folder sementara.
"""

import io
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from distrorelay.common import packages
from distrorelay.common.profile import Profile
from distrorelay.windows import kit_export
from distrorelay_linux import cli, dotfiles, pm, provision
from distrorelay_linux import kit as linux_kit
from distrorelay_linux.distro import detect
from distrorelay_linux.runner import FakeRunner, as_root

OS_RELEASE = {
    "fedora": 'NAME="Fedora Linux"\nVERSION_ID=41\nID=fedora\nPRETTY_NAME="Fedora Linux 41 (Workstation Edition)"\n',
    "nobara": "ID=nobara\nID_LIKE=\"rhel centos fedora\"\nVERSION_ID=41\n",
    "arch": 'NAME="Arch Linux"\nID=arch\nPRETTY_NAME="Arch Linux"\nBUILD_ID=rolling\n',
    "endeavouros": "ID=endeavouros\nID_LIKE=arch\n",
    "manjaro": "ID=manjaro\nID_LIKE=arch\n",
    "ubuntu": 'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\n',
    "linuxmint": "ID=linuxmint\nID_LIKE=\"ubuntu debian\"\nVERSION_ID=\"22\"\n",
    "kali": "ID=kali\nID_LIKE=debian\nVERSION_ID=\"2025.3\"\n",
    "opensuse": 'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n',
}
PACMAN_CONF = (
    "[options]\nArchitecture = auto\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n\n"
    "#[multilib-testing]\n#Include = /etc/pacman.d/mirrorlist\n\n#[multilib]\n#Include = /etc/pacman.d/mirrorlist\n"
)


@pytest.mark.parametrize(
    "name, family",
    [("fedora", "fedora"), ("nobara", "fedora"), ("arch", "arch"), ("endeavouros", "arch"), ("manjaro", "arch"),
     ("ubuntu", "debian"), ("linuxmint", "debian"), ("kali", "debian"), ("opensuse", None)],
)
def test_distro_family_from_os_release(name, family):
    distro = detect(OS_RELEASE[name])
    assert distro.family == family
    assert distro.id == ("opensuse-tumbleweed" if name == "opensuse" else name)


def test_root_commands_go_through_sudo_with_env():
    assert as_root(["dnf", "install", "-y", "git"], is_root=False).args == ("sudo", "dnf", "install", "-y", "git")
    apt = pm.Apt().install(["git"], is_root=False)
    assert apt.args[:2] == ("sudo", "DEBIAN_FRONTEND=noninteractive") and apt.env == ()
    as_root_apt = pm.Apt().install(["git"], is_root=True)
    assert as_root_apt.args[0] == "apt-get" and dict(as_root_apt.env) == {"DEBIAN_FRONTEND": "noninteractive"}
    assert pm.Pacman().install(["git"], is_root=False).args[:3] == ("sudo", "pacman", "-Syu")  # bukan -Sy saja


def test_rpmfusion_needs_a_numeric_fedora_version():
    command = pm.rpmfusion("41", is_root=True)
    assert command.args[-1].endswith("rpmfusion-nonfree-release-41.noarch.rpm")
    with pytest.raises(ValueError):
        pm.rpmfusion("rawhide; rm -rf /", is_root=True)


def test_enable_multilib_only_touches_the_multilib_section():
    new = pm.enable_multilib(PACMAN_CONF)
    assert "\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n" in new
    assert "#[multilib-testing]\n#Include" in new  # testing tetap dikomentari
    assert pm.enable_multilib(new) is None  # sudah aktif
    with pytest.raises(ValueError):
        pm.enable_multilib("[options]\n")


# --- Rencana setup -----------------------------------------------------------------------


def plan_for(name, profile, **kwargs):
    defaults = dict(is_root=False, has_paru=False, home=Path("/home/samuel"), stamp="20261022-150000",
                    workdir="/tmp/distrorelay-x", read_file=lambda path: PACMAN_CONF)
    defaults.update(kwargs)
    return provision.build_plan(profile, packages.builtin(), detect(OS_RELEASE[name]), **defaults)


def commands_of(plan):
    return {s.key: [c.display() for c in s.commands] for s in plan.steps}


PROFILE = Profile(
    native=["firefox", "git", "build-tools", "vscode"],
    flatpak=["com.spotify.Client"],
    aur=["visual-studio-code-bin"],
    repos=["rpmfusion", "flathub", "multilib", "universe"],
    dotfiles=["~/.bashrc"],
)


def test_fedora_plan():
    plan = plan_for("fedora", PROFILE)
    assert [s.key for s in plan.steps] == ["repo-rpmfusion", "native", "flathub", "flatpak"]
    cmds = commands_of(plan)
    assert cmds["native"] == ["sudo dnf install -y firefox git gcc gcc-c++ make autoconf automake pkgconf flatpak"]
    assert cmds["flatpak"] == ["sudo flatpak install -y --noninteractive flathub com.spotify.Client"]
    notes = " | ".join(plan.notes)
    assert "multilib dilewati" in notes and "universe dilewati" in notes and "'vscode' tidak tersedia" in notes
    assert "AUR hanya untuk keluarga Arch" in notes and "Dotfiles belum dipulihkan" in notes


def test_arch_plan_builds_paru_only_with_approval():
    plan = plan_for("endeavouros", PROFILE)
    assert [s.key for s in plan.steps] == ["repo-multilib", "native", "flathub", "flatpak", "paru", "aur"]
    paru = next(s for s in plan.steps if s.key == "paru")
    assert paru.approval and paru.commands[-1].args == ("makepkg", "-si", "--noconfirm")
    assert next(s for s in plan.steps if s.key == "aur").after == ("paru",)
    assert commands_of(plan)["repo-multilib"][1].startswith("sudo install -m 644 /tmp/distrorelay-x/pacman.conf /etc/pacman.conf")

    with_paru = plan_for("arch", PROFILE, has_paru=True)
    assert "paru" not in [s.key for s in with_paru.steps]
    as_root = plan_for("arch", PROFILE, is_root=True)
    assert "aur" not in [s.key for s in as_root.steps] and any("makepkg tidak boleh" in n for n in as_root.notes)


def test_ubuntu_plan_uses_id_specific_names():
    plan = plan_for("ubuntu", PROFILE)
    assert [s.key for s in plan.steps] == ["repo-universe", "native", "flathub", "flatpak"]
    native = commands_of(plan)["native"]
    assert native[0] == "sudo apt-get update"
    assert "firefox" in native[1].split() and "build-essential" in native[1].split()
    mint = plan_for("linuxmint", PROFILE)
    assert "repo-universe" not in [s.key for s in mint.steps]  # Mint sudah punya universe


def test_unsupported_distro_has_no_steps():
    plan = plan_for("opensuse", PROFILE)
    assert plan.steps == [] and "belum didukung" in plan.notes[0]


def test_distro_specific_packages_only_on_their_family():
    profile = Profile(native=["git"], distro_specific={"fedora": ["dnf-plugins-core"], "arch": ["pacman-contrib"]})
    fedora = plan_for("fedora", profile)
    assert "dnf-plugins-core" in commands_of(fedora)["native"][0] and "pacman-contrib" not in commands_of(fedora)["native"][0]
    assert any("khusus keluarga arch dilewati" in n for n in fedora.notes)


# --- Eksekusi ----------------------------------------------------------------------------


def test_execute_all_ok_and_text_outputs():
    plan = plan_for("fedora", PROFILE)
    runner = FakeRunner()
    results = provision.execute(plan, runner, approve=lambda q: True)
    assert [r.status for r in results] == ["ok"] * 4
    assert "Rencana setup untuk Fedora Linux 41" in provision.plan_text(plan)
    assert "[ok]" in provision.report_text(results)


def test_failed_batch_is_retried_per_package():
    plan = plan_for("fedora", Profile(native=["git", "nonexistent-pkg", "neovim"]))
    runner = FakeRunner()
    runner.add("sudo dnf install -y git nonexistent-pkg neovim", returncode=1)
    runner.add("sudo dnf install -y nonexistent-pkg", returncode=1)
    [result] = provision.execute(plan, runner, approve=lambda q: True)
    assert result.status == "sebagian" and result.message == "gagal: nonexistent-pkg"
    assert runner.ran()[-3:] == ["sudo dnf install -y git", "sudo dnf install -y nonexistent-pkg", "sudo dnf install -y neovim"]


def test_dependent_steps_are_skipped_after_failure_or_refusal():
    plan = plan_for("arch", PROFILE)
    runner = FakeRunner()
    runner.add("sudo flatpak remote-add", returncode=1)
    results = {r.key: r for r in provision.execute(plan, runner, approve=lambda q: False)}
    assert results["flathub"].status == "gagal"
    assert results["flatpak"].status == "dilewati" and "flathub" in results["flatpak"].message
    assert results["paru"].status == "dilewati" and results["aur"].status == "dilewati"
    assert not any(c.startswith("makepkg") or c.startswith("paru") for c in runner.ran())


def test_multilib_step_writes_staged_conf(tmp_path):
    plan = plan_for("arch", Profile(repos=["multilib"], native=["git"]), workdir=str(tmp_path))
    results = provision.execute(plan, FakeRunner(), approve=lambda q: True)
    assert results[0].status == "ok"
    assert "\n[multilib]\nInclude" in (tmp_path / "pacman.conf").read_text(encoding="utf-8")


# --- Dotfiles ----------------------------------------------------------------------------


def make_archive(path: Path, entries: dict) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            if content is None:
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/shadow"
                tar.addfile(info)
                continue
            data = content.encode()
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
    return path


def test_restore_dotfiles_safely(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "nvim").mkdir(parents=True)
    (home / ".bashrc").write_bytes(b"lama\n")
    (home / ".gitconfig").write_bytes(b"[user]\n")  # bytes: write_text di Windows menulis \r\n
    archive = make_archive(tmp_path / "dotfiles.tar.gz", {
        ".bashrc": "baru\n",
        ".gitconfig": "[user]\n",
        "./.config/nvim/init.lua": "-- nvim\n",
        "../keluar.txt": "x",
        "/etc/passwd": "x",
        ".ssh-link": None,
    })
    report = dotfiles.restore(archive, home, "20261022-150000")
    assert (home / ".bashrc").read_text() == "baru\n"
    assert (home / ".bashrc.distrorelay-20261022-150000.bak").read_text() == "lama\n"
    assert report.unchanged == [".gitconfig"] and ".config/nvim/init.lua" in report.restored
    assert not (tmp_path / "keluar.txt").exists() and not (home / ".ssh-link").exists()
    assert {name for name, _ in report.skipped} == {"../keluar.txt", "/etc/passwd", ".ssh-link"}
    assert "1 file lama di-rename" in report.summary()


def test_safe_member_path():
    assert dotfiles.safe_member_path("./.config//nvim/./init.lua") == ".config/nvim/init.lua"
    for bad in ("../x", "a/../../x", "/abs", "", ".", "a\\..\\b"):
        assert dotfiles.safe_member_path(bad) is None, bad


# --- Export kit (Windows) -> pakai kit (Linux) -----------------------------------------


def export(root: Path, profile=PROFILE, now=datetime(2026, 10, 22, 15, 0, 0)):
    return kit_export.export_kit(profile, packages.builtin(), root, now=now)


def test_exported_kit_loads_on_the_linux_side(tmp_path):
    result = export(tmp_path)
    assert result.ok and result.kit_dir == tmp_path / "DistroRelay" / "kit"
    kit = linux_kit.load(result.kit_dir)
    assert kit.profile == PROFILE and kit.packages.lookup("build-tools", "arch", "arch") == ["base-devel"]
    assert kit.backups_dir == tmp_path / "DistroRelay" / "linux-backups"
    for rel in ("bootstrap.sh", "distrorelay/__init__.py", "distrorelay/common/profile.py", "distrorelay_linux/cli.py"):
        assert (result.kit_dir / rel).is_file(), rel
    assert b"\r\n" not in (result.kit_dir / "bootstrap.sh").read_bytes()


def test_kit_verify_catches_tampering(tmp_path):
    kit_dir = export(tmp_path).kit_dir
    with open(kit_dir / "distrorelay_linux" / "pm.py", "a", encoding="utf-8") as handle:
        handle.write("\n# diubah\n")
    (kit_dir / "profile.json").unlink()
    problems = linux_kit.verify(kit_dir)
    assert any("pm.py: isinya berubah" in p for p in problems) and any("profile.json: hilang" in p for p in problems)
    with pytest.raises(linux_kit.KitError):
        linux_kit.load(kit_dir)


def test_re_export_keeps_previous_kit_and_never_touches_backups(tmp_path):
    backups = tmp_path / "DistroRelay" / "linux-backups" / "20261020-fedora"
    backups.mkdir(parents=True)
    (backups / "dotfiles.tar.gz").write_bytes(b"backup")
    export(tmp_path, now=datetime(2026, 10, 22, 15, 0, 0))
    second = export(tmp_path, Profile(name="kedua"), now=datetime(2026, 10, 22, 16, 0, 0))
    assert second.previous == tmp_path / "DistroRelay" / "kit.prev"
    assert linux_kit.load(second.previous).profile.name == "default"
    assert linux_kit.load(second.kit_dir).profile.name == "kedua"
    assert (backups / "dotfiles.tar.gz").read_bytes() == b"backup"
    assert not list((tmp_path / "DistroRelay").glob("kit.new-*"))


def test_export_refuses_a_foreign_kit_folder(tmp_path):
    (tmp_path / "DistroRelay" / "kit").mkdir(parents=True)
    (tmp_path / "DistroRelay" / "kit" / "catatan.txt").write_text("punya orang")
    with pytest.raises(kit_export.KitExportError):
        export(tmp_path)


def test_find_kits_newest_first(tmp_path):
    media = tmp_path / "run-media"
    export(media / "USB", now=datetime(2026, 10, 20, 10, 0, 0))
    export(media / "WINDOWS", now=datetime(2026, 10, 22, 10, 0, 0))
    (media / "kosong").mkdir()
    found = linux_kit.find_kits([media])
    assert [p.parent.parent.name for _, p in found] == ["WINDOWS", "USB"]


def test_linux_cli_dry_run_changes_nothing(tmp_path, monkeypatch, capsys):
    kit_dir = export(tmp_path).kit_dir
    monkeypatch.setattr("distrorelay_linux.distro.read", lambda: detect(OS_RELEASE["fedora"]))
    monkeypatch.setattr("distrorelay_linux.cli.os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("distrorelay_linux.kit.default_search_roots", lambda user=None: [])
    calls = []
    monkeypatch.setattr("distrorelay_linux.cli.SubprocessRunner.run", lambda self, *a, **k: calls.append(a))
    assert cli.main(["--kit", str(kit_dir), "setup", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Rencana setup untuk Fedora Linux 41" in output and "sudo dnf install -y" in output
    assert calls == []
    assert cli.main(["--kit", str(kit_dir), "info"]) == 0
    assert "semua hash cocok" in capsys.readouterr().out


def test_bootstrap_is_valid_posix_sh(tmp_path):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh tidak ada")
    script = export(tmp_path).kit_dir / "bootstrap.sh"
    result = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_windows_cli_profile_and_kit(tmp_path, monkeypatch, capsys):
    from distrorelay.cli.linux import cmd_kit, cmd_profile
    import argparse

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert cmd_profile(argparse.Namespace(profile_command="init", force=False)) == 0
    assert cmd_profile(argparse.Namespace(profile_command="init", force=False)) == 1  # sudah ada
    assert cmd_profile(argparse.Namespace(profile_command="show")) == 0
    out = capsys.readouterr().out
    assert "Profil 'laptop-utama'" in out and "Arch: firefox git neovim vlc base-devel" in out

    kit_dir = export(tmp_path / "usb").kit_dir
    assert cmd_kit(argparse.Namespace(kit_command="verify", directory=str(kit_dir))) == 0
    (kit_dir / "packages.json").write_text("{}")
    assert cmd_kit(argparse.Namespace(kit_command="verify", directory=str(kit_dir))) == 1
