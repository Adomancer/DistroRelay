## DistroRelay 0.7.0 (pre-release)

Toolkit distro-hopping untuk laptop dual-boot Windows 11 + Linux (GPT/UEFI): bersihkan sisa Linux lama (entri boot,
folder ESP, partisi), siapkan distro baru (ISO terverifikasi di USB Ventoy, ruang kosong dari C:), lalu pasang ulang
aplikasi + dotfiles lewat Linux kit.

> ⚠️ **Pre-release.** `uninstall`, `restore`, dan `shrink-c` **mengubah sistem**. Uninstall EFI (M2) sudah lolos test di
> VM; hapus partisi (M3), Linux kit (M4–M5), dan shrink C: (M6) baru lolos test otomatis. Coba di VM dulu, siapkan USB
> darurat, dan baca [docs/RECOVERY.md](https://github.com/Adomancer/DistroRelay/blob/main/docs/RECOVERY.md).

### Cara pakai

1. Unduh `distrorelay.exe` dan `distrorelay.exe.sha256`, lalu cocokkan hash-nya:
   `Get-FileHash .\distrorelay.exe -Algorithm SHA256`
2. Jalankan dari PowerShell / Windows Terminal, misalnya `.\distrorelay.exe selftest`. Tanpa argumen, dashboard TUI terbuka
   (minta hak Administrator lewat UAC).
3. `.exe` belum ditandatangani, jadi SmartScreen bisa memperingatkan: *More info → Run anyway*.

Tidak perlu Python. Untuk ISO manager butuh `gpgv` (`winget install GnuPG.GnuPG`, atau yang ikut Git for Windows).

### Isi

- **Scan read-only**: disk/partisi, isi ESP, entri boot (bcdedit + NVRAM mentah), header filesystem Linux, dan sisa
  instalasi yang dikelompokkan dengan bukti.
- **Uninstall bersih** (CLI + wizard TUI): backup per lapisan ke C: + drive eksternal, dry-run, Windows Boot Manager
  dijadikan pertama, hapus entri/folder ESP/partisi Linux dengan konfirmasi ketik, journal, post-check, menu Pulihkan.
- **Linux kit**: profil aplikasi/dotfiles, `setup` di Fedora/Arch/Debian-Ubuntu-Mint, `backup` dari distro lama
  (file sensitif selalu dienkripsi gpg dan hanya di USB).
- **ISO manager**: versi terbaru Fedora, Arch, Ubuntu LTS, Mint; unduhan yang bisa dilanjutkan; tanda tangan GPG
  dicek dengan kunci bawaan yang di-pin + SHA256; salin ke USB Ventoy dengan verifikasi baca ulang.
- **Shrink C:** untuk ruang distro baru, dengan batas aman (sisa kosong ≥ 20 GB).
- `selftest`: cek isi `.exe` tanpa mengubah sistem.
