"""Profil distro-hopping: aplikasi, repo, dan dotfiles yang dibawa dari satu distro ke distro berikutnya.

Di Windows profil diedit sebagai TOML (`profile.toml`); saat kit diekspor, profil ditulis sebagai `profile.json`
supaya Linux kit cukup memakai `json` dari stdlib.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PROFILE_VERSION = 1
FAMILIES = ("fedora", "arch", "debian")
KNOWN_REPOS = ("rpmfusion", "flathub", "multilib", "universe")

# Nama paket / nama logis masuk ke argumen package manager (bukan shell), tapi tetap dibatasi ke karakter yang
# memang dipakai nama paket: huruf, angka, @ (grup dnf), dan . _ + : -
_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._+:-]*$")
_FLATPAK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")


class ProfileError(ValueError):
    """Profil tidak valid. `problems` berisi semua masalah yang ditemukan, bukan hanya yang pertama."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def home_path_problem(path: str) -> str | None:
    """Path dotfile harus di dalam home: diawali '~/', tanpa '..' dan tanpa bagian kosong."""
    if not isinstance(path, str) or not path.startswith("~/"):
        return f"{path!r}: harus diawali ~/ (di dalam home)"
    parts = path[2:].rstrip("/").split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        return f"{path!r}: tidak boleh kosong, berisi '.', '..', atau '//'"
    if "\x00" in path or "\\" in path:
        return f"{path!r}: berisi karakter yang tidak diizinkan"
    return None


def home_relative(path: str) -> str:
    """'~/.config/nvim/' -> '.config/nvim'."""
    return path[2:].rstrip("/")


@dataclass
class Profile:
    name: str = "default"
    native: list[str] = field(default_factory=list)  # nama logis (dipetakan per distro lewat packages)
    flatpak: list[str] = field(default_factory=list)  # ID aplikasi Flathub
    aur: list[str] = field(default_factory=list)  # hanya di keluarga Arch
    repos: list[str] = field(default_factory=list)  # KNOWN_REPOS; dilewati kalau tidak relevan dengan distronya
    dotfiles: list[str] = field(default_factory=list)  # ~/... dibackup biasa
    sensitive: list[str] = field(default_factory=list)  # ~/... SELALU dienkripsi (M5)
    # Paket hasil backup (M5) yang tidak punya nama logis, per keluarga distro: hanya dipasang di keluarga yang sama.
    distro_specific: dict[str, list[str]] = field(default_factory=dict)
    # Hasil kurasi (M5): nama paket asli yang tidak pernah dibawa saat backup, supaya tidak perlu dikurasi ulang.
    exclude: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        found: list[str] = []
        if not isinstance(self.name, str) or not _PROFILE_NAME.match(self.name):
            found.append(f"meta.name {self.name!r}: 1-64 karakter huruf/angka/spasi/._-")
        for label, names, pattern in (
            ("apps.native", self.native, _NAME),
            ("apps.aur", self.aur, _NAME),
            ("apps.flatpak", self.flatpak, _FLATPAK_ID),
            ("apps.exclude", self.exclude, _NAME),
        ):
            found += _list_problems(label, names, pattern)
        found += _list_problems("repos.enable", self.repos, None)
        for repo in self.repos if isinstance(self.repos, list) else []:
            if repo not in KNOWN_REPOS:
                found.append(f"repos.enable: {repo!r} tidak dikenal (pilihan: {', '.join(KNOWN_REPOS)})")
        for label, paths in (("dotfiles.paths", self.dotfiles), ("dotfiles.sensitive", self.sensitive)):
            found += _list_problems(label, paths, None)
            for path in paths if isinstance(paths, list) else []:
                problem = home_path_problem(path)
                if problem:
                    found.append(f"{label}: {problem}")
        overlap = set(self.dotfiles or []) & set(self.sensitive or [])
        if overlap:
            found.append(f"dotfiles: {sorted(overlap)} ada di paths dan sensitive sekaligus (pilih salah satu)")
        if not isinstance(self.distro_specific, dict):
            found.append("distro_specific: harus tabel keluarga distro -> daftar paket")
        else:
            for family, names in self.distro_specific.items():
                if family not in FAMILIES:
                    found.append(f"distro_specific.{family}: keluarga distro tidak dikenal ({', '.join(FAMILIES)})")
                found += _list_problems(f"distro_specific.{family}", names, _NAME)
        return found

    def check(self) -> "Profile":
        problems = self.problems()
        if problems:
            raise ProfileError(problems)
        return self

    def to_dict(self) -> dict:
        return {
            "version": PROFILE_VERSION,
            "name": self.name,
            "apps": {"native": list(self.native), "flatpak": list(self.flatpak), "aur": list(self.aur),
                     "exclude": list(self.exclude)},
            "repos": list(self.repos),
            "dotfiles": {"paths": list(self.dotfiles), "sensitive": list(self.sensitive)},
            "distro_specific": {k: list(v) for k, v in sorted(self.distro_specific.items())},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        if not isinstance(data, dict):
            raise ProfileError(["profil harus berupa objek"])
        version = data.get("version", PROFILE_VERSION)
        if version != PROFILE_VERSION:
            raise ProfileError([f"versi profil {version!r} tidak didukung (yang didukung: {PROFILE_VERSION})"])
        apps = data.get("apps") or {}
        dotfiles = data.get("dotfiles") or {}
        wrong = [name for name, value in (("apps", apps), ("dotfiles", dotfiles)) if not isinstance(value, dict)]
        if wrong:
            raise ProfileError([f"{name}: harus tabel/objek, bukan daftar atau teks" for name in wrong])
        return cls(
            name=data.get("name", "default"),
            native=apps.get("native", []),
            flatpak=apps.get("flatpak", []),
            aur=apps.get("aur", []),
            repos=data.get("repos", []),
            dotfiles=dotfiles.get("paths", []),
            sensitive=dotfiles.get("sensitive", []),
            distro_specific=data.get("distro_specific", {}),
            exclude=apps.get("exclude", []),
        ).check()


def _list_problems(label: str, values, pattern) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return [f"{label}: harus daftar teks"]
    found = []
    if pattern is not None:
        found += [f"{label}: {v!r} bukan nama yang valid" for v in values if not pattern.match(v)]
    duplicates = sorted({v for v in values if values.count(v) > 1})
    if duplicates:
        found.append(f"{label}: dobel {duplicates}")
    return found


# --- JSON (Linux kit) ---------------------------------------------------------------


def to_json(profile: Profile) -> str:
    return json.dumps(profile.check().to_dict(), indent=2, ensure_ascii=False) + "\n"


def from_json(text: str) -> Profile:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProfileError([f"profile.json bukan JSON yang valid: {exc}"]) from exc
    return Profile.from_dict(data)


# --- TOML (Windows) -------------------------------------------------------------------


def from_toml(text: str) -> Profile:
    """Baca profile.toml. Butuh `tomllib` (Python 3.11+), jadi hanya dipakai di sisi Windows."""
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError([f"profile.toml tidak valid: {exc}"]) from exc
    unknown = sorted(set(data) - {"meta", "apps", "repos", "dotfiles", "distro_specific"})
    if unknown:
        raise ProfileError([f"bagian tidak dikenal: {unknown}"])
    not_tables = [name for name in ("meta", "apps", "repos", "dotfiles", "distro_specific")
                  if name in data and not isinstance(data[name], dict)]
    if not_tables:
        # mis. `repos = ["flathub"]` padahal yang dimaksud `[repos]` + `enable = ["flathub"]`
        raise ProfileError([f"{name}: harus tabel `[{name}]`, bukan nilai biasa" for name in not_tables])
    meta = data.get("meta", {})
    return Profile.from_dict(
        {
            "version": meta.get("version", PROFILE_VERSION),
            "name": meta.get("name", "default"),
            "apps": data.get("apps", {}),
            "repos": data.get("repos", {}).get("enable", []),
            "dotfiles": data.get("dotfiles", {}),
            "distro_specific": data.get("distro_specific", {}),
        }
    )


def _toml_list(values: list[str]) -> str:
    # String JSON (dengan escape \" \\ \n \uXXXX) juga basic string TOML yang valid.
    if not values:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(v, ensure_ascii=False)},\n" for v in values) + "]"


def to_toml(profile: Profile) -> str:
    profile.check()
    lines = [
        "# Profil DistroRelay: dibawa ke setiap distro baru lewat Linux kit.",
        "# Nama di apps.native adalah nama logis; terjemahannya per distro ada di packages.toml.",
        "",
        "[meta]",
        f"name = {json.dumps(profile.name, ensure_ascii=False)}",
        f"version = {PROFILE_VERSION}",
        "",
        "[apps]",
        f"native = {_toml_list(profile.native)}",
        f"flatpak = {_toml_list(profile.flatpak)}",
        "# Hanya dipasang di keluarga Arch, lewat paru (dibangun dari AUR dengan persetujuanmu).",
        f"aur = {_toml_list(profile.aur)}",
        "# Paket asli yang tidak pernah dibawa saat backup (hasil kurasi); hapus dari sini kalau berubah pikiran.",
        f"exclude = {_toml_list(profile.exclude)}",
        "",
        "[repos]",
        f"# Pilihan: {', '.join(KNOWN_REPOS)}. Yang tidak relevan dengan distronya dilewati.",
        f"enable = {_toml_list(profile.repos)}",
        "",
        "[dotfiles]",
        f"paths = {_toml_list(profile.dotfiles)}",
        "# Selalu dienkripsi (gpg) dan salinannya selalu ke USB.",
        f"sensitive = {_toml_list(profile.sensitive)}",
    ]
    if profile.distro_specific:
        lines += ["", "# Paket hasil backup yang tidak punya nama logis: hanya dipasang di keluarga distro yang sama.",
                  "[distro_specific]"]
        lines += [f"{family} = {_toml_list(names)}" for family, names in sorted(profile.distro_specific.items())]
    return "\n".join(lines) + "\n"


def merge_update(profile: Profile, update: dict) -> tuple[Profile, list[str]]:
    """Gabungkan hasil backup Linux (`profile_update` di manifest backup) ke profil. Hanya menambah, urutan lama tetap.

    Hasilnya (profil baru, daftar perubahan untuk ditampilkan). Paket yang ada di `exclude` tidak ikut ditambahkan.
    """

    def union(old: list[str], new: list[str], label: str, skip=()) -> list[str]:
        added = [x for x in new if x not in old and x not in skip]
        if added:
            changes.append(f"+ {label}: {', '.join(added)}")
        return list(old) + added

    changes: list[str] = []
    exclude = union(profile.exclude, update.get("exclude", []), "apps.exclude")
    specific = {family: list(names) for family, names in profile.distro_specific.items()}
    for family, names in sorted((update.get("distro_specific") or {}).items()):
        specific[family] = union(specific.get(family, []), names, f"distro_specific.{family}", exclude)
    merged = Profile(
        name=profile.name,
        native=union(profile.native, update.get("native", []), "apps.native", exclude),
        flatpak=union(profile.flatpak, update.get("flatpak", []), "apps.flatpak"),
        aur=union(profile.aur, update.get("aur", []), "apps.aur", exclude),
        repos=list(profile.repos),
        dotfiles=list(profile.dotfiles),
        sensitive=list(profile.sensitive),
        distro_specific=specific,
        exclude=exclude,
    )
    new_excludes = [name for name in exclude if name not in profile.exclude]
    kept = [name for name in new_excludes if name in merged.native or name in merged.aur]
    if kept:  # exclude hanya berlaku saat backup; yang sengaja ditulis di profil tetap dipasang
        changes.append(f"  (tetap di profil karena ditulis manual: {', '.join(kept)}; hapus sendiri kalau tidak mau)")
    return merged.check(), changes


def template() -> Profile:
    """Profil awal yang wajar untuk diedit."""
    return Profile(
        name="laptop-utama",
        native=["firefox", "git", "neovim", "vlc", "build-tools", "curl", "htop"],
        flatpak=[],
        aur=[],
        repos=["flathub"],
        dotfiles=["~/.bashrc", "~/.gitconfig", "~/.config/nvim"],
        sensitive=[],
    )
