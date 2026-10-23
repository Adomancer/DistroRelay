## DistroRelay 0.7.1 (pre-release)

Toolkit distro-hopping untuk laptop dual-boot Windows 11 + Linux (GPT/UEFI): bersihkan sisa Linux lama (entri boot,
folder ESP, partisi), siapkan distro baru (ISO terverifikasi di USB Ventoy, ruang kosong dari C:), lalu pasang ulang
aplikasi + dotfiles lewat Linux kit.

> ⚠️ **Pre-release.** `uninstall`, `restore`, dan `shrink-c` **mengubah sistem**. Uninstall EFI (M2) sudah lolos test di
> VM; hapus partisi (M3), Linux kit (M4–M5), dan shrink C: (M6) baru lolos test otomatis. Coba di VM dulu, siapkan USB
> darurat, dan baca [docs/RECOVERY.md](https://github.com/Adomancer/DistroRelay/blob/main/docs/RECOVERY.md).

### Perubahan sejak 0.7.0

Perbaikan dari dua review kode (review tiga ronde oleh Fable 5.1, lalu code review lengkap). Tidak ada temuan yang
membuat DistroRelay menghapus sesuatu di luar rencana; yang diperbaiki adalah kasus pinggir, pengaman, dan performa.

**Uninstall dan Pulihkan**

- Backup yang gagal salah satu cek saat dibuat tidak bisa dipakai untuk uninstall; daftar backup menandainya.
- Salinan backup di drive eksternal dibaca ulang langsung dari perangkat (bukan dari cache Windows) dan harus sama
  persis dengan backup lokal. `--backup` hanya menerima ID backup, bukan path folder.
- Flashdisk/HDD lain boleh dicolok atau dicabut di antara backup dan uninstall: layout hanya dibandingkan untuk disk
  Windows dan disk yang partisinya dihapus.
- Manifest backup yang rusak menghasilkan pesan gagal, bukan crash. Backup yang gagal di tengah tidak meninggalkan
  folder setengah jadi.
- Sesi yang terputus atau gagal bisa ditandai tuntas (`journal --tuntaskan <sesi>` atau layar Journal, ketik
  `TUNTAS`). Uninstall yang semua item terhapusnya sudah dikembalikan lewat Pulihkan otomatis dianggap tuntas.
- Pulihkan dan shrink C: dicatat di journal dan bisa dihentikan dengan rapi (Ctrl+C). Path dari manifest backup
  divalidasi sebelum folder ESP dipulihkan.
- Fallback `EFI\Boot\bootx64.efi` hanya dikembalikan ke milik Windows kalau memang tidak dipakai lagi. Folder alat
  pabrikan (mis. `EFI\HP`) tidak pernah disentuh.
- Path dari disk dikirim ke PowerShell sebagai base64, jadi nama berisi kutip tipografis tidak bisa memecah perintah.

**Scan**

- Rujukan GRUB `(,gptN)` dipetakan lewat tabel GPT asli (diverifikasi CRC), bukan nomor partisi Windows. Tanpa bukti
  positif, hasilnya "tidak pasti".
- Entri boot dari disk lain tidak lagi digabung dengan folder ESP yang namanya sama.

**Linux kit**

- Backup Linux tidak lagi menggantikan pernyataan "DATA SUDAH DIBACKUP" saat uninstall.
- File sensitif tidak bocor lewat symlink/hardlink dan dipulihkan dengan permission ketat; cadangan `.bak` tidak
  ditimpa; apt dipanggil dengan `--no-remove`.
- Verifikasi backup membaca file per 1 MiB (tidak lagi utuh ke RAM), arsip dotfiles dibatasi 2 GiB, dan wizard
  uninstall tidak lagi beku saat menghitung cakupan backup Linux.
- Di Arch, pemasangan per paket menjalankan `pacman -Syu` sekali, lalu `pacman -S --needed` per paket.

**ISO manager**

- `iso check` mengambil feed keempat distro bersamaan (timeout 20 detik).
- SHA256 dihitung sambil mengunduh, dan unduhan menolak redirect ke alamat non-HTTPS.
- File di USB yang isinya berbeda tidak ditimpa, dan salinan ISO dibaca ulang langsung dari perangkat. Kunci Ubuntu
  DSA-1024 yang lama tidak lagi dipercaya.

**Lain-lain**

- Perintah yang meminta UAC menunggu jendela Administrator selesai, lalu keluar dengan kode yang sama.
- Log harian yang lebih dari 30 hari dihapus otomatis; lama baca NVRAM dicatat di log.
- `.exe` dibuild dengan action GitHub yang dipin ke SHA commit dan versi paket dari `packaging/constraints.txt`.

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
