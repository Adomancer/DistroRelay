"""Argumen baris perintah `distrorelay`."""

from __future__ import annotations

import argparse

from .. import __version__

ISO_DISTROS = ("fedora", "arch", "ubuntu", "mint")

# Perintah yang hanya membaca file DistroRelay sendiri tidak perlu hak Administrator.
NO_ADMIN_COMMANDS = {"verify-backup", "journal", "profile", "kit", "iso", "selftest"}

# Di jendela Administrator (relaunch), jendela ditahan sampai Enter supaya hasilnya terbaca.
PAUSE_COMMANDS = {"scan", "plan", "backup", "uninstall", "restore", "fast-startup", "shrink-c"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distrorelay",
        description=(
            "Toolkit distro-hopping untuk dual-boot Windows + Linux. "
            "scan/plan/backup/verify-backup/journal hanya membaca; uninstall dan restore MENGUBAH sistem (coba di VM dulu)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"DistroRelay {__version__}")
    parser.add_argument(
        "--no-elevate", action="store_true", help="jangan minta hak Administrator (scan jadi terbatas)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log lebih detail")
    parser.add_argument("--elevated-child", action="store_true", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("tui", help="buka dashboard (default)")
    scan = commands.add_parser("scan", help="scan read-only dan tampilkan ringkasan di terminal")
    scan.add_argument("--json", metavar="FILE", help="simpan laporan lengkap ke file JSON")
    scan.add_argument(
        "--raw-dir",
        metavar="DIR",
        help="simpan output mentah perintah (untuk fixture test; nomor seri disk disamarkan)",
    )
    plan = commands.add_parser("plan", help="dry-run rencana uninstall (tidak menjalankan apa pun)")
    plan.add_argument(
        "--select", action="append", default=[], metavar="KEY",
        help="key instalasi (mis. efi:fedora, termasuk partisinya) atau ref komponen; boleh diulang",
    )
    plan.add_argument("--extend-c", action="store_true",
                      help="perluas C: ke ruang bekas partisi (hanya kalau partisinya tepat sesudah C:)")
    backup = commands.add_parser("backup", help="preflight + backup per lapisan + verifikasi (tidak mengubah sistem)")
    target = backup.add_mutually_exclusive_group()
    target.add_argument(
        "--usb", "--drive", dest="usb", metavar="HURUF",
        help="huruf drive eksternal (flashdisk/HDD) tujuan salinan backup, mis. E",
    )
    target.add_argument(
        "--local-only",
        action="store_true",
        help="mode coba: simpan hanya di C:, tanpa USB (belum memenuhi syarat eksekusi M2b)",
    )
    verify = commands.add_parser("verify-backup", help="verifikasi ulang folder backup terhadap manifest-nya")
    verify.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroRelay\\backups\\20261021-193000")
    uninstall = commands.add_parser(
        "uninstall", help="JALANKAN rencana uninstall (menghapus entri boot & folder ESP), dengan banyak konfirmasi"
    )
    uninstall.add_argument("--select", action="append", default=[], metavar="KEY", help="sama seperti di `plan`")
    uninstall.add_argument("--backup", metavar="ID", help="ID backup yang sudah ada salinannya di drive eksternal")
    uninstall.add_argument("--extend-c", action="store_true", help="sama seperti di `plan`")
    restore = commands.add_parser("restore", help="pulihkan partisi Linux, folder ESP, dan/atau entri boot dari backup")
    restore.add_argument("--backup", metavar="ID", required=True)
    restore.add_argument("--esp-item", action="append", default=[], metavar="PATH", help="mis. EFI\\fedora")
    restore.add_argument("--boot-entry", action="append", default=[], metavar="NAMA",
                         help="nama entri (mis. Fedora), atau nomornya (mis. Boot0007) kalau namanya dipakai lebih dari satu entri")
    restore.add_argument("--partition", action="append", default=[], metavar="ID",
                         help="partisi Linux dari backup (mis. disk0-p5 atau GUID partisinya)")
    journal = commands.add_parser("journal", help="tampilkan sesi eksekusi, termasuk yang terputus di tengah")
    journal.add_argument("--tuntaskan", metavar="SESI",
                         help="tandai sesi terputus/gagal sebagai sudah ditangani (ketik TUNTAS), mis. 20261022-101500")

    profile = commands.add_parser("profile", help="profil distro-hopping (aplikasi, repo, dotfiles) untuk Linux kit")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    init = profile_commands.add_parser("init", help="buat profile.toml awal untuk diedit")
    init.add_argument("--force", action="store_true", help="timpa profile.toml yang sudah ada")
    profile_commands.add_parser("show", help="cek profil dan tampilkan ringkasannya")
    profile_import = profile_commands.add_parser("import", help="gabungkan hasil `backup` dari Linux ke profil")
    profile_import.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroRelay\\linux-backups\\<id>")
    profile_import.add_argument("--yes", action="store_true", help="tanpa konfirmasi")

    kit = commands.add_parser("kit", help="Linux kit: export ke USB / partisi NTFS, verifikasi")
    kit_commands = kit.add_subparsers(dest="kit_command", required=True)
    export = kit_commands.add_parser("export", help="tulis kit + profil ke <drive>:\\DistroRelay\\kit")
    export.add_argument("--drive", metavar="HURUF", help="drive tujuan, mis. E (flashdisk)")
    export.add_argument("--ntfs", action="store_true", help="juga ke C:\\DistroRelay\\kit (partisi Windows)")
    kit_verify = kit_commands.add_parser("verify", help="cek ulang hash semua file kit")
    kit_verify.add_argument("directory", metavar="FOLDER", help="mis. E:\\DistroRelay\\kit")

    fast = commands.add_parser("fast-startup", help="lihat / ubah Fast Startup (harus mati supaya Linux bisa menulis ke NTFS)")
    switch = fast.add_mutually_exclusive_group()
    switch.add_argument("--off", action="store_true", help="matikan Fast Startup")
    switch.add_argument("--on", action="store_true", help="nyalakan lagi Fast Startup")

    iso = commands.add_parser("iso", help="ISO manager: cek versi terbaru, unduh + verifikasi GPG/SHA256, salin ke Ventoy")
    iso_commands = iso.add_subparsers(dest="iso_command", required=True)
    iso_check = iso_commands.add_parser("check", help="versi terbaru tiap distro vs ISO di USB Ventoy")
    iso_check.add_argument("--drive", metavar="HURUF", help="USB Ventoy (default: dicari dari label 'Ventoy')")
    iso_download = iso_commands.add_parser("download", help="unduh ISO terbaru lalu verifikasi (bisa dilanjutkan)")
    iso_download.add_argument("distro", choices=ISO_DISTROS)
    iso_download.add_argument("--edition", help="mis. KDE (Fedora) atau xfce (Mint)")
    iso_verify = iso_commands.add_parser("verify", help="verifikasi ulang ISO yang sudah diunduh (tanpa internet)")
    iso_verify.add_argument("distro", choices=ISO_DISTROS)
    iso_verify.add_argument("--version", dest="iso_version", metavar="VERSI", help="default: unduhan terbaru")
    iso_verify.add_argument("--edition", help="kalau ada beberapa edisi yang diunduh")
    iso_copy = iso_commands.add_parser("copy", help="verifikasi ulang lalu salin ISO ke USB Ventoy")
    iso_copy.add_argument("distro", choices=ISO_DISTROS)
    iso_copy.add_argument("--version", dest="iso_version", metavar="VERSI", help="default: unduhan terbaru")
    iso_copy.add_argument("--edition", help="kalau ada beberapa edisi yang diunduh")
    iso_copy.add_argument("--drive", metavar="HURUF", help="USB Ventoy (default: dicari dari label 'Ventoy')")
    iso_commands.add_parser("keys", help="kunci GPG bawaan per distro dan lokasi gpgv")

    shrink = commands.add_parser(
        "shrink-c", help="kecilkan C: untuk ruang distro baru (MENGUBAH partisi, coba di VM dulu)"
    )
    shrink.add_argument("--size", type=float, metavar="GB", help="berapa GB dibebaskan; tanpa ini hanya menampilkan info")
    commands.add_parser("selftest", help="cek isi paket/.exe: data bawaan, Linux kit, keyring ISO, TUI (tidak mengubah sistem)")
    return parser
