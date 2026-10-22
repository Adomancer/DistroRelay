"""Profil (TOML di Windows, JSON di Linux) dan peta nama paket per distro (M4)."""

import pytest

from distrorelay.common import packages
from distrorelay.common import profile as profile_mod
from distrorelay.common.profile import Profile, ProfileError


def full_profile() -> Profile:
    return Profile(
        name="laptop utama",
        native=["firefox", "git", "build-tools", "vscode"],
        flatpak=["com.spotify.Client"],
        aur=["visual-studio-code-bin"],
        repos=["rpmfusion", "flathub", "multilib"],
        dotfiles=["~/.bashrc", "~/.config/nvim/"],
        sensitive=["~/.ssh"],
        distro_specific={"fedora": ["fedora-workstation-backgrounds"]},
        exclude=["steam"],
    )


def test_toml_and_json_round_trip():
    original = full_profile()
    text = profile_mod.to_toml(original)
    assert "[distro_specific]" in text and 'name = "laptop utama"' in text
    assert profile_mod.from_toml(text) == original
    assert profile_mod.from_json(profile_mod.to_json(original)) == original


def test_template_is_valid():
    assert profile_mod.template().problems() == []


def test_strings_with_quotes_and_unicode_survive_toml():
    profile = Profile(dotfiles=['~/.config/app "beta"/config', "~/Dokumén/catatan.txt"])
    assert profile_mod.from_toml(profile_mod.to_toml(profile)).dotfiles == profile.dotfiles


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"native": ["firefox; rm -rf /"]}, "bukan nama yang valid"),
        ({"native": ["git", "git"]}, "dobel"),
        ({"flatpak": ["spotify"]}, "bukan nama yang valid"),
        ({"repos": ["ppa:foo"]}, "tidak dikenal"),
        ({"dotfiles": ["/etc/passwd"]}, "harus diawali ~/"),
        ({"dotfiles": ["~/../root"]}, "'..'"),
        ({"dotfiles": ["~/"]}, "tidak boleh kosong"),
        ({"dotfiles": ["~/.ssh"], "sensitive": ["~/.ssh"]}, "paths dan sensitive"),
        ({"distro_specific": {"gentoo": ["x"]}}, "keluarga distro tidak dikenal"),
        ({"name": ""}, "meta.name"),
        ({"native": "firefox"}, "harus daftar teks"),
    ],
)
def test_invalid_profiles_are_rejected(change, expected):
    data = full_profile().to_dict()
    for key, value in change.items():
        if key in ("native", "flatpak", "aur"):
            data["apps"][key] = value
        elif key in ("dotfiles", "sensitive"):
            data["dotfiles"]["paths" if key == "dotfiles" else "sensitive"] = value
            if key == "dotfiles" and "sensitive" not in change:
                data["dotfiles"]["sensitive"] = []
        else:
            data[key] = value
    with pytest.raises(ProfileError) as error:
        Profile.from_dict(data)
    assert any(expected in p for p in error.value.problems), error.value.problems


def test_unknown_toml_sections_and_versions_are_rejected():
    with pytest.raises(ProfileError, match="tidak dikenal"):
        profile_mod.from_toml("[meta]\nname = 'x'\n[aps]\nnative = []\n")
    with pytest.raises(ProfileError, match="versi profil"):
        profile_mod.from_toml("[meta]\nname = 'x'\nversion = 99\n")


# --- Peta paket ------------------------------------------------------------------------


def test_builtin_map_precedence_id_then_family_then_all():
    m = packages.builtin()
    assert m.lookup("firefox", "debian", "debian") == ["firefox-esr"]
    assert m.lookup("firefox", "ubuntu", "debian") == ["firefox"]  # ID distro menang atas keluarga
    assert m.lookup("git", "fedora", "fedora") == ["git"]  # dari `all`
    assert m.lookup("build-tools", "endeavouros", "arch") == ["base-devel"]
    assert m.lookup("tidak-ada", "fedora", "fedora") is None


def test_resolve_dedups_skips_unavailable_and_keeps_unknown_names():
    m = packages.builtin()
    result = m.resolve(["build-tools", "vscode", "my-tool", "git", "git"], "fedora", "fedora")
    assert result.packages[:3] == ["gcc", "gcc-c++", "make"] and "my-tool" in result.packages
    assert result.packages.count("git") == 1
    assert result.skipped == ["vscode"]


def test_reverse_maps_installed_packages_back_to_logical_names():
    m = packages.builtin()
    logical, unmapped = m.reverse(["git", "base-devel", "neovim", "linux-zen", "nodejs"], "arch", "arch")
    assert {"git", "build-tools", "neovim"} <= set(logical)
    assert "nodejs" not in logical  # butuh nodejs + npm di Arch
    assert unmapped == ["linux-zen", "nodejs"]


def test_user_map_overrides_per_name():
    user = packages.from_dict({"firefox": {"debian": ["firefox"]}, "my-app": {"all": ["my-app-bin"]}})
    merged = packages.builtin().merged(user)
    assert merged.lookup("firefox", "debian", "debian") == ["firefox"]
    assert merged.lookup("firefox", "fedora", "fedora") == ["firefox"]
    assert merged.lookup("my-app", "arch", "arch") == ["my-app-bin"]


def test_bad_maps_are_rejected():
    with pytest.raises(packages.PackageMapError):
        packages.from_dict({"x": {"fedora": ["ok", "bad name"]}})
    with pytest.raises(packages.PackageMapError):
        packages.from_json('{"version": 2, "packages": {}}')
    m = packages.builtin()
    assert packages.from_json(m.to_json()).entries == m.entries
