## DistroRelay 0.7.2 (pre-release)

Toolkit distro-hopping untuk laptop dual-boot Windows 11 + Linux (GPT/UEFI): bersihkan sisa Linux lama (entri boot,
folder ESP, partisi), siapkan distro baru (ISO terverifikasi di USB Ventoy, ruang kosong dari C:), lalu pasang ulang
aplikasi + dotfiles lewat Linux kit.

> ⚠️ **Pre-release.** `uninstall`, `restore`, dan `shrink-c` **mengubah sistem**. Uninstall EFI (M2), hapus dan
> pulihkan partisi (M3), serta shrink C: (M6) sudah lolos test di VM; Linux kit (M4–M5) baru lolos test otomatis dan
> dry-run di WSL. Coba di VM dulu, siapkan USB darurat, dan baca
> [docs/RECOVERY.md](https://github.com/Adomancer/DistroRelay/blob/main/docs/RECOVERY.md).

### Perubahan sejak 0.7.1

Perbaikan dari uji VM dan uji di laptop asli (tanpa mengubah partisi). Tidak ada temuan yang membuat DistroRelay
menghapus sesuatu di luar rencana: setiap kali, gerbang atau konfirmasi tetap menolak.

**Backup dan uninstall**

- Salinan backup di drive eksternal sekarang benar-benar ditulis ke perangkat sebelum diverifikasi. Sebelumnya, drive
  yang dicabut segera sesudah backup bisa menyimpan file yang rusak padahal verifikasi bilang ok. Hasil backup memuat
  cek baru "Salinan USB sudah tertulis ke perangkat" dan mengingatkan untuk mencabut drive lewat Safely Remove.
- Reboot di antara backup dan uninstall tidak lagi membatalkan backup: `EFI\Microsoft\Boot\bootstat.dat`, yang
  ditulis Windows setiap boot, tidak dihitung sebagai perubahan ESP di gerbang maupun post-check.
- Sesi uninstall yang gagal hanya dianggap tuntas oleh uninstall sukses yang ikut menghapus item yang tertinggal,
  bukan oleh sembarang uninstall sukses.
- `verification.json` atau `manifest.json` yang disimpan ulang dengan BOM (mis. dari PowerShell 5.1) tetap terbaca.
- Pesan yang lebih jelas: `--select` yang sudah tidak ada sesudah crash disertai daftar kandidat yang tersisa; layout
  yang berubah sebelum hapus partisi menyebut kemungkinan partisi baru (mis. dari Disk Management); `shrink-c` yang
  ditolak hanya menampilkan alasannya.

**Linux kit**

- Backup di keluarga Debian/Ubuntu tidak lagi membawa paket inti sistem dan pustaka (dibaca dari metadata dpkg).
  Di image hasil debootstrap seperti WSL, checklist turun dari 134 paket menjadi 7.
- `setup` menyebut backup terbaru yang rusak, termasuk kalau backup yang lebih lama dipakai sebagai gantinya.

**ISO manager dan lain-lain**

- `iso download` memberi tahu kalau unduhan dilanjutkan dari file `.part`, dan ISO yang ditolak disebut lokasi serta
  ukurannya.
- Jawaban yang di-pipe dari PowerShell 5.1 (mis. `'ya' | distrorelay …`) tidak lagi gagal karena BOM. Kata konfirmasi
  seperti `HAPUS` tetap harus diketik persis.

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
