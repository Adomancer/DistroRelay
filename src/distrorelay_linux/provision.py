"""`setup` di distro baru: susun rencana dari profil, tampilkan, lalu jalankan langkah demi langkah.

Urutan: repo sistem -> paket native -> Flathub -> Flatpak -> AUR -> dotfiles -> laporan. Semua langkah menambah
(tidak ada yang menghapus), jadi langkah yang gagal tidak menghentikan langkah lain yang tidak bergantung padanya;
hasilnya dilaporkan per langkah. Paket native dipasang sekaligus, dan kalau gagal dicoba satu per satu supaya
ketahuan paket mana yang bermasalah.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from distrorelay.common.profile import Profile

from . import dotfiles, pm
from .distro import Distro
from .runner import Command

# Repo yang relevan per distro. Yang tidak cocok dicatat sebagai "dilewati", bukan error.
_REPO_FOR = {"rpmfusion": ("fedora",), "multilib": ("arch",), "universe": ("ubuntu",)}


@dataclass
class Step:
    key: str
    title: str
    commands: list[Command] = field(default_factory=list)
    # Langkah Python (mis. edit pacman.conf, pulihkan dotfiles): pesan, atau (status, pesan) kalau hasilnya sebagian.
    action: Callable[[], object] | None = None
    after: tuple[str, ...] = ()  # langkah yang harus berhasil dulu
    each: list[str] = field(default_factory=list)  # paket untuk dicoba satu per satu kalau perintah gagal
    each_command: Callable[[list[str]], Command] | None = None
    each_prepare: list[Command] = field(default_factory=list)  # sekali, sebelum dicoba satu per satu
    approval: str | None = None  # pertanyaan tambahan sebelum langkah ini (mis. membangun paket dari AUR)


@dataclass
class SetupPlan:
    distro: Distro
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    key: str
    title: str
    status: str  # "ok" | "gagal" | "sebagian" | "dilewati"
    message: str = ""


def build_plan(
    profile: Profile,
    package_map,
    distro: Distro,
    *,
    is_root: bool,
    has_paru: bool,
    home: Path,
    stamp: str,
    workdir: str,
    backup_dir: Path | None = None,
    sensitive_file: Path | None = None,
    decrypt: Callable[[Path], bytes] | None = None,
    read_file: Callable[[str], str] = lambda path: Path(path).read_text(encoding="utf-8"),
) -> SetupPlan:
    """`backup_dir` = folder backup dari distro lama (dotfiles.tar.gz); `sensitive_file` + `decrypt` = file sensitif
    terenkripsi dan cara mendekripsinya (gpg, passphrase diminta saat langkahnya jalan)."""
    plan = SetupPlan(distro)
    if distro.family is None:
        plan.notes.append(f"Distro {distro.describe} belum didukung (keluarga yang didukung: Fedora, Arch, Debian/Ubuntu)")
        return plan
    family, backend = distro.family, pm.backend_for(distro.family)
    wants_flatpak = bool(profile.flatpak) or "flathub" in profile.repos

    # 1. Repo sistem
    for repo in profile.repos:
        if repo == "flathub":
            continue
        targets = _REPO_FOR.get(repo, ())
        if distro.id not in targets and family not in targets:
            plan.notes.append(f"Repo {repo} dilewati: tidak relevan untuk {distro.describe}")
            continue
        if repo == "rpmfusion":
            try:
                plan.steps.append(Step("repo-rpmfusion", "Aktifkan RPM Fusion (free + nonfree)",
                                       [pm.rpmfusion(distro.version_id, is_root)]))
            except ValueError as exc:
                plan.notes.append(f"Repo rpmfusion dilewati: {exc}")
        elif repo == "universe":
            plan.steps.append(Step("repo-universe", "Aktifkan repo universe", pm.universe(is_root)))
        elif repo == "multilib":
            step = _multilib_step(is_root, stamp, workdir, read_file)
            if isinstance(step, str):
                plan.notes.append(step)
            else:
                plan.steps.append(step)

    # 2. Paket native (+ flatpak sendiri kalau ada aplikasi Flatpak)
    resolution = package_map.resolve(profile.native, distro.id, family)
    native = list(resolution.packages)
    for name in resolution.skipped:
        plan.notes.append(f"Paket '{name}' tidak tersedia di {distro.describe}, dilewati")
    for package in profile.distro_specific.get(family, []):
        if package not in native:
            native.append(package)
    for other, names in sorted(profile.distro_specific.items()):
        if other != family and names:
            plan.notes.append(f"{len(names)} paket khusus keluarga {other} dilewati (tidak ada nama logisnya)")
    if wants_flatpak and "flatpak" not in native:
        native.append("flatpak")
    if native:
        # Tetap dicoba walau ada repo yang gagal: paket dari repo itu akan gagal sendiri dan dilaporkan.
        plan.steps.append(
            Step(
                "native",
                f"Pasang {len(native)} paket lewat {backend.name}",
                backend.refresh(is_root) + [backend.install(native, is_root)],
                each=native,
                each_command=lambda packages: backend.install_one(packages, is_root),
                each_prepare=backend.prepare_each(is_root),
            )
        )

    # 3-4. Flathub + aplikasi Flatpak
    if wants_flatpak:
        plan.steps.append(Step("flathub", "Tambahkan remote Flathub", [pm.flathub_remote(is_root)],
                               after=("native",) if native else ()))
    if profile.flatpak:
        plan.steps.append(
            Step(
                "flatpak",
                f"Pasang {len(profile.flatpak)} aplikasi Flatpak",
                [pm.flatpak_install(profile.flatpak, is_root)],
                after=("flathub",),
                each=list(profile.flatpak),
                each_command=lambda ids: pm.flatpak_install(ids, is_root),
            )
        )

    # 5. AUR
    if profile.aur:
        if family != "arch":
            plan.notes.append(f"{len(profile.aur)} paket AUR dilewati: AUR hanya untuk keluarga Arch")
        elif is_root:
            plan.notes.append("Paket AUR dilewati: makepkg tidak boleh dijalankan sebagai root; jalankan setup sebagai user biasa")
        else:
            after = ()
            if not has_paru:
                plan.steps.append(
                    Step(
                        "paru",
                        "Bangun dan pasang paru-bin dari AUR",
                        pm.paru_bootstrap(workdir, is_root),
                        approval="Paket AUR dibangun dari skrip komunitas (bukan repo resmi). Bangun paru-bin sekarang?",
                    )
                )
                after = ("paru",)
            plan.steps.append(
                Step(
                    "aur",
                    f"Pasang {len(profile.aur)} paket AUR lewat paru",
                    [pm.aur_install(profile.aur)],
                    after=after,
                    each=list(profile.aur),
                    each_command=pm.aur_install,
                )
            )

    # 6. Dotfiles + file sensitif. Dijalankan sebagai root (sudo sh bootstrap.sh setup), home-nya /root dan file
    # yang ditulis milik root, jadi langkah ini dilewati; kit sendiri meminta sudo hanya untuk perintah paket.
    archive = Path(backup_dir) / "dotfiles.tar.gz" if backup_dir is not None else None
    as_root = is_root and (archive is not None or sensitive_file is not None)
    if as_root:
        plan.notes.append("Dotfiles dan file sensitif tidak dipulihkan karena setup dijalankan sebagai root; "
                          "jalankan `sh bootstrap.sh setup` tanpa sudo sebagai pengguna biasa")
        archive, sensitive_file = None, None
    if archive is not None and archive.is_file():
        plan.steps.append(
            Step(
                "dotfiles",
                f"Pulihkan dotfiles dari {archive}",
                action=lambda: restore_outcome(dotfiles.restore(archive, home, stamp)),
            )
        )
    elif profile.dotfiles and not as_root:
        plan.notes.append("Dotfiles belum dipulihkan: belum ada backup (dibuat dengan `backup` di distro lama)")
    if sensitive_file is not None and decrypt is not None:
        plan.steps.append(
            Step(
                "sensitive",
                f"Pulihkan file sensitif dari {sensitive_file} (gpg, minta passphrase)",
                action=lambda: restore_outcome(dotfiles.restore(decrypt(sensitive_file), home, stamp, private=True)),
            )
        )
    elif profile.sensitive and backup_dir is not None and not as_root:
        plan.notes.append("File sensitif tidak dipulihkan: tidak ada salinan terenkripsinya di backup ini atau di USB")
    return plan


def _multilib_step(is_root: bool, stamp: str, workdir: str, read_file) -> Step | str:
    try:
        new_text = pm.enable_multilib(read_file(pm.PACMAN_CONF))
    except (OSError, ValueError) as exc:
        return f"Repo multilib dilewati: {exc}"
    if new_text is None:
        return "Repo multilib sudah aktif"
    staged = f"{workdir}/pacman.conf"
    backup = f"{pm.PACMAN_CONF}.distrorelay-{stamp}.bak"

    def write_staged() -> str:
        Path(staged).write_text(new_text, encoding="utf-8")
        return f"[multilib] diaktifkan, cadangan {backup}"

    commands = pm.install_file(staged, pm.PACMAN_CONF, backup, is_root)
    return Step("repo-multilib", f"Aktifkan multilib di {pm.PACMAN_CONF} (cadangan: {backup})", commands,
                action=write_staged)


def plan_text(plan: SetupPlan) -> str:
    lines = [f"Rencana setup untuk {plan.distro.describe}", ""]
    if not plan.steps:
        lines.append("  (tidak ada langkah)")
    for number, step in enumerate(plan.steps, 1):
        lines.append(f"  {number}. {step.title}")
        for cmd in step.commands:
            lines.append(f"       $ {cmd.display()}")
        if step.approval:
            lines.append(f"       (minta persetujuan: {step.approval})")
    if plan.notes:
        lines += ["", "Catatan"] + [f"  - {note}" for note in plan.notes]
    return "\n".join(lines) + "\n"


def execute(
    plan: SetupPlan,
    runner,
    *,
    approve: Callable[[str], bool],
    progress: Callable[[str], None] = lambda message: None,
) -> list[StepResult]:
    results: dict[str, StepResult] = {}
    for number, step in enumerate(plan.steps, 1):
        progress(f"[{number}/{len(plan.steps)}] {step.title}")
        blocked = [key for key in step.after if key in results and results[key].status not in ("ok", "sebagian")]
        if blocked:
            results[step.key] = StepResult(step.key, step.title, "dilewati", f"butuh langkah {', '.join(blocked)}")
            continue
        if step.approval and not approve(step.approval):
            results[step.key] = StepResult(step.key, step.title, "dilewati", "tidak disetujui")
            continue
        results[step.key] = _run_step(step, runner)
    return list(results.values())


def restore_outcome(report) -> tuple[str, str]:
    """Status langkah pulihkan dotfiles: file yang dilewati disebut (path: alasan), bukan hanya dihitung."""
    message = report.summary()
    if report.skipped:
        shown = "; ".join(f"{path}: {why}" for path, why in report.skipped[:5])
        more = f" (+{len(report.skipped) - 5} lagi)" if len(report.skipped) > 5 else ""
        message += f". Dilewati: {shown}{more}"
    if not report.skipped:
        return "ok", message
    return ("sebagian" if report.restored or report.unchanged else "gagal"), message


def _run_step(step: Step, runner) -> StepResult:
    try:
        message = step.action() if step.action is not None else ""
    except Exception as exc:  # langkah Python gagal = langkah ini gagal, sisanya tetap dicoba
        return StepResult(step.key, step.title, "gagal", f"{type(exc).__name__}: {exc}")
    if isinstance(message, tuple):
        status, text = message
        if status != "ok":
            return StepResult(step.key, step.title, status, text)
        message = text
    for cmd in step.commands:
        result = runner.run(cmd, capture=False)
        if result.ok:
            continue
        if not step.each or step.each_command is None:
            return StepResult(step.key, step.title, "gagal", f"`{cmd.display()}` keluar dengan kode {result.returncode}")
        for prepare in step.each_prepare:
            prepared = runner.run(prepare, capture=False)
            if not prepared.ok:
                return StepResult(step.key, step.title, "gagal",
                                  f"`{prepare.display()}` keluar dengan kode {prepared.returncode}")
        failed = [name for name in step.each if not runner.run(step.each_command([name]), capture=False).ok]
        if len(failed) == len(step.each):
            return StepResult(step.key, step.title, "gagal", f"semua gagal: {', '.join(failed)}")
        return StepResult(step.key, step.title, "sebagian", f"gagal: {', '.join(failed)}" if failed else "berhasil per paket")
    return StepResult(step.key, step.title, "ok", message)


def report_text(results: list[StepResult]) -> str:
    marks = {"ok": "[ok]      ", "sebagian": "[sebagian]", "gagal": "[GAGAL]   ", "dilewati": "[dilewati]"}
    lines = ["Hasil setup"]
    for result in results:
        line = f"  {marks.get(result.status, result.status)} {result.title}"
        lines.append(line + (f": {result.message}" if result.message else ""))
    return "\n".join(lines) + "\n"


def new_workdir() -> str:
    return tempfile.mkdtemp(prefix="distrorelay-")
