# Skenario test di VM UEFI

Wajib lolos sebelum M2b/M3 dijalankan di laptop asli. Firmware VM tidak sama dengan firmware laptop,
jadi VM menurunkan risiko tapi tidak menghapusnya: dry-run + post-check di laptop asli tetap wajib.

## Persiapan

- Hypervisor: **VirtualBox 7** (EFI, NVRAM persisten, vTPM, snapshot) atau **VMware Workstation Pro**
  (gratis untuk pemakaian personal; biasanya lebih mulus kalau WSL2 aktif). Hyper-V tidak ada di Windows Home.
- Ruang kosong ±60–80 GB (disk dinamis). Windows di VM tidak perlu diaktivasi.
- Snapshot dasar:
  - `dualboot-grub`: Windows + Fedora (GRUB + shim)
  - `dualboot-sdboot`: Windows + Arch (systemd-boot, kernel di ESP)
  - `dualboot-dua-linux`: Windows + 2 distro
- **Cek dulu:** ubah urutan boot → restore snapshot → urutan harus kembali. Kalau tidak kembali, berarti snapshot
  tidak menyimpan NVRAM, jadi backup file `.nvram` VM secara manual sebelum tiap skenario.
- VirtualBox sangat lambat kalau Hyper-V aktif di host (WSL2, Memory Integrity): `VBox.log` berisi
  `fall back to NEM`. Matikan dengan `bcdedit /set hypervisorlaunchtype off` + restart (WSL2 tidak jalan sampai
  dikembalikan dengan `bcdedit /set hypervisorlaunchtype auto`), atau pakai VMware.

## Jalur cepat: VM Windows saja + sisa Linux palsu

Tanpa install Linux. [`fake_leftovers.py`](fake_leftovers.py) meniru kondisi laptop asli di dalam VM: partisi #5
XBOOTLDR dengan header ext4 `/boot`, `EFI\fedora`, `EFI\Arch`, `grub\` yatim, `EFI\HP`, fallback berisi shim,
serta entri "Fedora" dan "Arch" di urutan pertama. Script ini menolak jalan di luar VirtualBox/VMware.

```powershell
# Di VM (PowerShell Administrator, folder repo). Disk kedua yang masih kosong ikut di-format jadi E:.
.venv\Scripts\python.exe tests\vm\fake_leftovers.py
# Matikan VM → snapshot "sisa-palsu" → nyalakan, lalu jalankan scan/plan/backup/uninstall seperti di bawah
```

Isi `.efi`-nya palsu, jadi firmware gagal menjalankan entri Fedora/Arch lalu lanjut ke Windows Boot Manager.
Kalau VM berhenti di layar firmware, tekan F2 → Boot Manager → Windows Boot Manager.

Yang **tidak** teruji di jalur ini: skenario 1, 5, 6, dan bagian "Linux bisa boot lagi" di skenario 9. Itu butuh
Linux sungguhan, jadi wajib sebelum menghapus Linux yang **masih terpasang**, bukan untuk membersihkan sisa.

## Skenario

Kolom "Lolos" mencatat versi kode saat skenario itu lolos. Setiap perubahan di jalur eksekusi (mis. refactor
`UninstallFlow` di M2c fase A) berarti skenario 2, 3, 7, 7b, 7c, 9, dan 10 **diulang dengan kode terbaru** sebelum
laptop asli. Skenario 8 juga, karena prosedurnya memakai folder `esp\` hasil backup.

| # | Skenario | Yang dicek | Untuk | Lolos |
|---|---|---|---|---|
| 1 | Uninstall Linux yang masih utuh (Fedora + GRUB) | VM langsung boot ke Windows, post-check hijau | M2b, M3 | [ ] |
| 2 | Kondisi seperti laptop asli: root sudah di-wipe, `/boot` + folder ESP + entri tersisa | Terdeteksi "kemungkinan sisa" dengan alasan benar; setelah dibersihkan langsung boot ke Windows | M1b, M2b | [x] fase A (UninstallFlow), 2026-10-22, sisa palsu |
| 3 | Urutan boot Linux duluan | `SetWindowsFirst` jalan pertama dan benar | M2b | [x] fase A (UninstallFlow), 2026-10-22, sisa palsu |
| 4 | Entri merujuk `.efi` yang hilang, atau folder tanpa entri | Status yatim / tidak pasti benar | M1b, M2b | [x] v0.7.1, 2026-10-24, sisa palsu: entri "Hantu" (`bcdedit /copy`, path `\EFI\hantu\grubx64.efi`) → "[?] tidak pasti", "File .efi-nya sudah TIDAK ADA di ESP (entri yatim)"; folder `EFI\yatim` (GRUB prefix `(,gpt4)`) tanpa entri → "kemungkinan sisa" dengan alasan slot GPT 4 = NTFS |
| 5 | Arch + systemd-boot (kernel, `loader/`, UKI di ESP) | File dikelompokkan ke instalasi yang benar | M1b, M2b | [ ] |
| 6 | Dua Linux terinstall, hapus satu | Milik distro lain tidak tersentuh, distro itu masih bisa boot | M2b, M3 | [ ] |
| 7 | Gagal di tengah (`DISTRORELAY_FAIL_AFTER=<aksi>`, build dev) + VM dimatikan paksa | Journal mendeteksi, post-check & Pulihkan bekerja, Windows tetap boot | M2b | [x] fase A (UninstallFlow), 2026-10-22: crash setelah langkah 1 + mati paksa → Arch tidak kembali, TERPUTUS, dituntaskan dengan backup baru, post-check 8/8. **v0.7.1, 2026-10-24**: crash sesudah langkah 3 (`grub`) + mati paksa 8 detik kemudian → boot normal, journal "TERPUTUS saat langkah 3 … sedang berjalan"; `--select` lama ditolak "Tidak dikenal" (foldernya sudah terhapus), dituntaskan dengan `--select disk0-p6` + `LANJUT`, post-check 9/9, 3 sesi "dituntaskan oleh sesi …" |
| 7b | Ctrl+C saat `uninstall` sedang jalan | Langkah yang sedang jalan selesai dulu, lalu berhenti; journal "dihentikan"; `uninstall` berikutnya minta LANJUT; Windows tetap boot | M2c | [x] fase A (UninstallFlow), 2026-10-22: perintah diketik langsung → langkah 2 diselesaikan lalu berhenti, journal "dihentikan". Catatan: lewat pipeline/script PowerShell, Ctrl+C MEMATIKAN proses (journal TERPUTUS). **v0.7.1, 2026-10-24**: Ctrl+C saat langkah 4 → "Dihentikan pengguna (Ctrl+C) setelah langkah 4 dari 9", `uninstall` berikutnya meminta `LANJUT` |
| 7c | Jendela PowerShell ditutup (X) saat `uninstall` sedang jalan | Sama dengan 7b sejauh Windows memberi waktu (±5 detik); kalau tidak sempat, journal TERPUTUS | M2c | [x] fase A (UninstallFlow), 2026-10-22: Alt+F4 saat langkah 2 → langkah selesai, journal "dihentikan" tertulis sebelum proses ditutup; ESP tertinggal ter-mount di Y: dan dipakai ulang di run berikutnya. **v0.7.1, 2026-10-24**: Alt+F4 saat langkah 2 → "Jendela ditutup / Windows logoff atau shutdown setelah langkah 2 dari 6" (dihentikan); ESP lagi-lagi tertinggal di Y: |
| 8 | Boot rusak sengaja (`EFI\Microsoft` dihapus di VM) | `docs/RECOVERY.md` berhasil sampai Windows boot lagi (**latihan untuk kamu**) | M2b | [ ] |
| 9 | `RestoreEspFiles` + `RestoreBootEntry` + `RestorePartition` setelah skenario 1 | Linux bisa boot lagi; setelah 2× reboot entri yang dipulihkan tidak jadi dobel (di VirtualBox, entri buatan `bcdedit /copy` pernah dibuat ulang tiap boot) | M2b, M3 | [~] fase A (UninstallFlow), 2026-10-22: EFI\Arch + EFI\fedora + entri (nama & Boot####) dipulihkan, stabil 2× reboot (1 paksa); nama dobel ditolak dengan pilihan nomor. Linux boot & partisi belum. **v0.7.1, 2026-10-24** (sesudah NVRAM di-reset): `EFI\fedora` + `EFI\Arch` + `grub` (hash cocok) + entri Arch & Fedora + partisi `disk0-p6` (UUID filesystem sama), 10/10 ok; sesudah 2× reset paksa tetap 1 entri Arch + 1 Fedora. Linux boot tetap belum (butuh Linux sungguhan) |
| 10 | NVRAM di-reset (hapus file `.nvram` VM) setelah uninstall | Boot ke Windows lewat fallback, dengan dan tanpa `RestoreFallbackToWindows` | M2b | [x] fase A (UninstallFlow), 2026-10-22: semua Boot#### dihapus → desktop Windows lewat fallback. **v0.7.1, 2026-10-24**: `VBoxManage modifynvram inituefivarstore` sesudah uninstall (NVRAM tanpa entri apa pun) → desktop Windows lewat fallback. Varian "tanpa `RestoreFallbackToWindows`" belum |

## Cara menjalankan di VM (M2b)

Di dalam VM Windows (dual-boot dengan Linux), clone repo, buat venv, lalu:

```powershell
# 1. Backup dengan salinan ke drive eksternal (di VM: virtual disk kedua, misal E:)
.venv\Scripts\python.exe -m distrorelay backup --drive E

# 2. Lihat kandidat & rencana
.venv\Scripts\python.exe -m distrorelay plan
.venv\Scripts\python.exe -m distrorelay plan --select efi:fedora

# 3. Jalankan (jawab "vm" saat ditanya)
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --backup <ID>

# Skenario 7 (crash di tengah): hanya aktif kalau dua variabel ini di-set
$env:DISTRORELAY_DEV = "1"; $env:DISTRORELAY_FAIL_AFTER = "3"
.venv\Scripts\python.exe -m distrorelay uninstall --select efi:fedora --backup <ID>
.venv\Scripts\python.exe -m distrorelay journal

# Skenario 9 (pulihkan)
.venv\Scripts\python.exe -m distrorelay restore --backup <ID> --esp-item EFI\fedora --boot-entry Fedora
```

## Gate M2c fase B: lewat wizard TUI

Wizard memakai `UninstallFlow`/`RestoreFlow` yang sama dengan CLI. Bedanya ada di cara input dan cara berhenti:
Textual mematikan *processed input* di konsol, jadi Ctrl+C sampai sebagai tombol, bukan sinyal. Ulangi dari snapshot
`siap-uninstall`, jalankan `.venv\Scripts\python.exe -m distrorelay` langsung di jendela PowerShell (bukan lewat pipeline):

| # | Lewat TUI | Yang dicek | Lolos |
|---|---|---|---|
| 2 + 3 | `u` → vm → centang `efi:fedora`, `efi:arch`, `grubdir:grub` (spasi) → preflight + centang USB darurat → backup ke E: → `HAPUS` | Post-check hijau; restart langsung masuk Windows; dashboard sesudahnya tidak lagi menampilkan Fedora/Arch | [x] 2026-10-22: dituntaskan setelah 7b/7q/7c (`LANJUT`, rencana menyusut 8 → 6 → 5 → 3 langkah), post-check 8/8, journal: 3 sesi "dituntaskan oleh" sesi hijau; reset paksa → langsung desktop Windows |
| 7b | Ctrl+C saat langkah 2–3 sedang jalan | Langkah yang sedang jalan selesai lalu berhenti; journal "dihentikan"; `u` berikutnya minta `LANJUT` | [x] 2026-10-22: Ctrl+C saat langkah 2 → "setelah langkah 2 dari 8"; banner kuning + layar `LANJUT` muncul |
| 7c | Tutup jendela (X / Alt+F4) saat eksekusi | Journal "dihentikan" (atau TERPUTUS kalau lewat ±5 detik); Windows tetap boot | [x] 2026-10-22: Alt+F4 saat langkah 2 → `session-end` "Jendela ditutup… setelah langkah 2 dari 5" < 1 detik sesudahnya |
| 7q | Ctrl+Q saat eksekusi | Aplikasi tidak keluar; berhenti dengan rapi seperti 7b | [x] 2026-10-22: "Dihentikan pengguna (Ctrl+Q) setelah langkah 1 dari 6", aplikasi tetap terbuka |
| 7 | `DISTRORELAY_DEV=1`, `DISTRORELAY_FAIL_AFTER=3`, lalu wizard sampai `HAPUS` | TUI menampilkan error, aplikasi tetap hidup, journal TERPUTUS, banner kuning di dashboard | [ ] (opsional; jalur crash sama dengan fase A, tampilannya diuji Pilot) |
| 9 | `p` → pilih backup → centang `EFI\fedora` + entri Fedora → `PULIHKAN` | Folder + entri kembali, tidak dobel setelah 2× reboot | [~] 2026-10-22: backup 20261022-125219 → `EFI\fedora` (6 file, hash cocok) + 1 entri Fedora, 4/4 ok, dashboard menampilkan 1 entri. Cek 2× reboot terputus karena VM freeze (NEM); lanjutkan dari snapshot `fase-b-sesudah-restore` |

Catatan gate fase B: host masih menjalankan Hyper-V (Memory Integrity / HVCI aktif), jadi VirtualBox jalan di mode NEM
dan sesekali freeze total (juga sebelum DistroRelay dijalankan). Script bantu: `tesvm\vmkeys.ps1` (keyboard + screenshot),
`tesvm\reboot-check.ps1` (reset + `bcdedit /enum firmware`). Temuan: dashboard 1 baris lebih tinggi dari layar → baris
status tergulung keluar di terminal sungguhan (diperbaiki dengan CSS, ada test regresi).

## Gate M3: hapus & pulihkan partisi (lewat TUI)

Mulai dari snapshot `siap-uninstall` (sisa palsu dari `fake_leftovers.py` sudah termasuk partisi XBOOTLDR 2 GB; di VM
biasanya `disk0-p6`), lalu pasang kode terbaru dengan `Z:\setup.ps1`. Memilih instalasi Fedora di wizard sekarang ikut
mencentang partisinya. Partisi itu berperan `/boot`, jadi cukup ketik ID partisinya, tanpa pernyataan backup data.

| # | Lewat TUI | Yang dicek | Lolos |
|---|---|---|---|
| M3-1 | `u` → vm → centang `efi:fedora`, `efi:arch`, `grubdir:grub` → preflight → backup → ketik ID partisi + `HAPUS` | Langkah terakhir "Hapus partisi"; post-check hijau termasuk "Partisi yang dihapus sudah tidak ada" dan "Layout disk lain tidak berubah"; Disk Management menampilkan 2 GB unallocated; restart langsung masuk Windows | [x] v0.7.1, 2026-10-24, lewat CLI (`uninstall --select efi:fedora --select efi:arch --select grubdir:grub`, flow sama): 9/9 langkah ok, langkah terakhir hapus `disk0-p6`, post-check 9/9; `Get-Partition` tanpa p6, partisi lain identik. Restart (reset paksa sesudah skenario 7) → desktop Windows dalam 100 detik |
| M3-2 | Di layar konfirmasi, ketik ID partisi yang salah (mis. `disk0-p4`) | Ditolak "Belum cocok", tidak ada yang diubah | [x] v0.7.1, 2026-10-24, lewat CLI: "Tidak cocok. Dibatalkan, tidak ada yang diubah."; layout identik |
| M3-3 | `p` → backup yang dibuat sebelum M3-1 → centang partisinya → `PULIHKAN` | Partisi kembali dengan offset/ukuran/tipe sama, "UUID filesystem sama"; dashboard menampilkannya lagi sebagai `/boot` Fedora | [x] v0.7.1, 2026-10-24, lewat CLI (`restore --partition disk0-p6`): offset/ukuran/tipe sama, "UUID filesystem sama" (GUID partisi GPT baru); `scan` menampilkan XBOOTLDR ext4 "terakhir di-mount di /boot", peran boot |
| M3-4 | Ulangi M3-1, tapi setelah gerbang lolos buat partisi kecil baru di ruang kosong lewat Disk Management, baru ketik `HAPUS` | Langkah partisi berhenti "Layout disk … tidak sama"; partisi Linux masih ada | [x] v0.7.1, 2026-10-24, lewat CLI + wrapper yang membuat partisi 100 MB tepat sebelum `HAPUS` dikirim: langkah 1–5 (entri, folder) jalan, langkah 6 "[GAGAL] … Layout disk sebelum partisi dihapus tidak sama … Langkah partisi dibatalkan", `disk0-p6` masih ada, post-check menandai partisi + layout |
| M3-5 | Beri partisi Linux drive letter (Disk Management → Change Drive Letter), lalu scan ulang | Partisi tidak bisa dicentang: "punya drive letter / mount point" | [x] v0.7.1, 2026-10-24, lewat CLI (`Set-Partition -NewDriveLetter P`, lalu `plan`): "Diblokir: disk0-p6: punya drive letter / mount point (sedang dipakai Windows)", tidak ada langkah hapus partisi |
| M3-6 (opsional) | Layout dengan partisi Linux tepat sesudah C: (mis. VM tanpa Recovery di antaranya) → centang "Perluas C:" | C: bertambah, post-check "C: diperluas"; sesudahnya Pulihkan menolak partisi itu "ruangnya sudah dipakai" | [x] v0.7.1, 2026-10-24: `shrink-c --size 3` lalu XBOOTLDR 2 GB palsu tepat sesudah C: (`disk0-p7`); `plan --extend-c` memuat "Perluas C:" + peringatan tidak bisa dipulihkan; uninstall 2/2 ok, C: 95.6 → 98.6 GB, post-check 10/10; Pulihkan: "Ruangnya sudah dipakai disk0-p4" |

Di laptop asli, ruang bekas `disk0-p5` **tidak** bersebelahan dengan C: (ada `disk0-p4` Windows Recovery di antaranya),
jadi opsi Perluas C: tidak muncul dan ruangnya dibiarkan unallocated untuk distro berikutnya.

## Gate M4–M5: Linux kit + backup

**Tanpa VM (WSL, mis. Kali).** Hanya `setup --dry-run`, jadi tidak ada paket yang dipasang:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| K-1 | Windows: `distrorelay profile init` → `profile show` → `kit export --ntfs` | Kit di `C:\DistroRelay\kit`, `kit verify C:\DistroRelay\kit` ok | [x] v0.7.1, 2026-10-25, laptop: profil baru (belum ada sebelumnya), 20 file, "Semua file kit cocok dengan manifest" |
| K-2 | WSL: `cd /mnt/c/DistroRelay/kit && sh bootstrap.sh info` | Distro terdeteksi (Kali → keluarga debian), "semua hash cocok" | [x] v0.7.1, 2026-10-25, WSL `kali-linux` (Python 3.13.7): "ID kali, keluarga debian", "semua hash cocok" |
| K-3 | WSL: `sh bootstrap.sh backup` → checklist → ketik beberapa nomor | Paket sistem (linux-image, grub, systemd, …) tidak muncul di checklist; backup di `C:\DistroRelay\linux-backups\<id>`. Karena kit di NTFS (bukan USB), file sensitif ditolak dengan catatan | [~] v0.7.1, 2026-10-25: backup `20261025-090713-kali` di `C:\DistroRelay\linux-backups`, kurasi `1 2` → `exclude`. `linux-image`/`grub`/`systemd` tidak muncul (WSL memang tidak punya), tapi checklist berisi 134 paket termasuk `libc6`, `init`, `udev`, `login`, `passwd`, `perl-base` (temuan T-7: `apt-mark showmanual` di image debootstrap menandai paket Priority required/important sebagai manual). Kali tanpa `gpg`, jadi catatannya "gpg tidak ada", bukan catatan NTFS; cabang NTFS belum teruji |
| K-4 | WSL: `sh bootstrap.sh backups` lalu `sh bootstrap.sh setup --dry-run` | Backup `[ok]`; rencana memuat "Pulihkan dotfiles dari …" | [x] v0.7.1, 2026-10-25: `[ok]`; rencana 3 langkah (apt 8 paket, Flathub, "Pulihkan dotfiles dari …"), catatan file sensitif tidak dipulihkan |
| K-5 | Windows: `distrorelay profile import C:\DistroRelay\linux-backups\<id>` → `ya` | Perubahan tampil, `profile.toml.bak-*` dibuat, `profile show` memuat paket baru + `exclude` | [x] v0.7.1, 2026-10-25: `+ apps.exclude`, `+ distro_specific.debian` (130, karena T-7), `+ apps.native: fastfetch, nano`; `profile.toml.bak-20261025-090824`. Catatan: `'ya' \|` dari PowerShell 5.1 di host ini mengirim BOM (`'﻿ya'`) → "Dibatalkan"; lewat `cmd /c "echo ya\| …"` jalan |
| K-6 | Ubah 1 byte `dotfiles.tar.gz` di backup itu, ulangi K-4 dan K-5 | `backups` menandai RUSAK, `setup` tidak memakainya, `profile import` menolak | [x] v0.7.1, 2026-10-25: byte 1292 di-XOR → `[RUSAK: dotfiles.tar.gz: isinya berubah (hash tidak cocok)]`, rencana tanpa langkah dotfiles, `profile import` "tidak bisa dipakai", profil tidak berubah; file dikembalikan sesudahnya. Catatan: `setup` menulis "belum ada backup", padahal ada tapi rusak |

**Di Linux sungguhan (VM dual-boot atau install sungguhan, belakangan):**

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| L-1 | Fedora: `sh bootstrap.sh setup` sungguhan | Repo, paket, dan Flatpak terpasang; laporan akhir masuk akal | [ ] |
| L-2 | Arch: `setup` dengan paket AUR, setujui bootstrap `paru` | `paru` dan paket AUR terpasang | [ ] |
| L-3 | Ubuntu 22.04 atau lebih baru: `setup` | apt + Flatpak jalan di Python 3.10 | [ ] |
| L-4 | Distro lama, kit di USB, `~/.ssh` di `dotfiles.sensitive`: `sh bootstrap.sh backup` | `sensitive.tar.gz.gpg` ada di USB; tidak ada salinan tanpa enkripsi | [ ] |
| L-5 | Distro lain: `sh bootstrap.sh setup --backup latest` | `~/.ssh` kembali dengan permission 700/600 (`.pub` 644) | [ ] |
| L-6 | Windows: wizard uninstall untuk partisi root yang punya backup L-4 di drive eksternal | Ada catatan "paket + dotfiles sudah dibackup", tapi "DATA SUDAH DIBACKUP" tetap diminta | [ ] |

## Gate M6: ISO manager + shrink C:

**Tanpa VM (di laptop, tidak mengubah sistem).** Butuh internet; ISO Arch ±1,5 GB adalah unduhan terkecil:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| I-1 | `distrorelay iso keys` | 4 distro + fingerprint tampil, `gpgv:` menunjuk ke file yang ada | [x] v0.7.1, 2026-10-25, laptop (`.exe`): 4 distro, 9 fingerprint, `gpgv: C:\Program Files\Git\usr\bin\gpgv.exe` |
| I-2 | `distrorelay iso check` (USB Ventoy dicabut) | Versi terbaru 4 distro tampil; catatan "USB Ventoy tidak terdeteksi" | [x] v0.7.1, 2026-10-25: Fedora 44, Arch 2026.10.01, Ubuntu 26.04.1, Mint 22.3 + "USB Ventoy tidak terdeteksi" |
| I-3 | `distrorelay iso download arch`, Ctrl+C di tengah, jalankan lagi | Unduhan lanjut dari posisi terakhir (tidak mulai dari 0); akhirnya tanda tangan "sah, oleh Pierre Schmitz" + SHA256 `[ok]` | [x] v0.7.1, 2026-10-25, laptop (`.exe`): Ctrl+C sungguhan (`GenerateConsoleCtrlEvent` ke konsol unduhan) pada 315,6 MB → "Unduhan dihentikan. Jalankan perintah yang sama lagi untuk melanjutkan.", exit 130, `.part` disimpan; dijalankan lagi → "sah, oleh Pierre Schmitz" + SHA256 `[ok]`. Diulang di I-5 dengan pencatat ukuran: Ctrl+C pada 526 MB, saat dilanjutkan `.part` tidak pernah di bawah 526.385.152 B (tidak mulai dari 0). Catatan: output tidak menyebut "melanjutkan dari …" (hanya terlihat di progress bar) |
| I-4 | Ubah 1 byte ISO di `%LOCALAPPDATA%\DistroRelay\iso\arch\...` (mis. pakai HxD), lalu `iso verify arch` | `[GAGAL]`; ISO disisihkan jadi `.rejected` | [x] v0.7.1, 2026-10-25: byte 800.000.000 di-XOR → `[GAGAL] Tanda tangan GPG: … TIDAK sah (BADSIG)` dalam 4,8 detik, exit 1, ISO jadi `.iso.rejected` (1,6 GB, tidak dihapus otomatis) |
| I-5 | `iso download arch` lagi, lalu colok USB Ventoy → `iso check` | Unduh ulang dari awal + lolos; `check` menampilkan isi USB dan status Arch | [~] v0.7.1, 2026-10-25: unduh ulang mulai dari 0 B (ada `.rejected`), Ctrl+C di 526 MB, dilanjutkan, tanda tangan + SHA256 `[ok]`. Bagian USB Ventoy belum (tidak ada USB) |
| I-6 | `distrorelay iso copy arch` | Tanda tangan dicek ulang, salin + "Baca ulang dari USB" `[ok]`; ISO Arch lama (kalau ada) tetap di USB; `iso check` → "sudah terbaru" | [ ] |
| I-7 | Boot laptop/VM dari USB Ventoy itu, pilih ISO Arch | Menu Ventoy menampilkan ISO, dan ISO bisa boot | [ ] |
| I-8 | (opsional, ±2,7 GB) `iso download fedora` → `iso copy fedora` | Sama seperti I-3/I-6 dengan CHECKSUM clearsigned Fedora | [ ] |
| I-9 | `distrorelay shrink-c` (tanpa `--size`) | Ukuran C:, ruang kosong, dan "maksimal yang bisa dibebaskan" masuk akal dibanding Disk Management. Tidak ada yang berubah | [x] v0.7.1, 2026-10-25, laptop (admin, `--no-elevate`): "C: 474 GB, kosong 16.3 GB. Bisa dibebaskan maksimal 0 B (batas Windows 852 MB, dan C: tetap kosong ≥ 20.0 GB)". Pembanding: `Get-PartitionSupportedSize` SizeMin 507.636.977.664 → batas 893.022.208 B, `Get-Volume` sisa 17.456.795.648 B; layout `Get-Partition` sebelum = sesudah |

**Di VM (snapshot dulu), karena mengubah partisi:**

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| S-1 | `distrorelay shrink-c --size 10` → ketik `kecilkan` (huruf kecil) | Dibatalkan, tidak ada yang berubah | [x] v0.7.1, 2026-10-24: "Dibatalkan", layout identik |
| S-2 | `shrink-c --size 10` → ketik `KECILKAN` | C: berkurang 10 GB, ruang kosong 10 GB muncul tepat sesudah C: di Disk Management; `journal` menampilkan sesi selesai + post-check ok | [x] v0.7.1, 2026-10-24: C: berkurang tepat 10 GiB, offset sama, celah tepat sesudah C:; journal "selesai, post-check ok". Tanpa sengaja jalan 2× (skrip uji) → total 20 GiB, sesi kedua juga lolos |
| S-3 | `shrink-c --size <lebih dari maksimal>` | `[GAGAL]` dengan alasan (batas Windows / sisa kosong 20 GB), tidak ada yang berubah | [x] v0.7.1, 2026-10-24: 2× `[GAGAL]` (batas Windows + sisa kosong). Catatan: ringkasan rencana tetap tercetak dengan angka negatif ("C: jadi -10630462439424 B") |
| S-4 | Reboot VM | Windows boot normal, `chkdsk C:` (read-only) bersih | [x] v0.7.1, 2026-10-24: boot ke desktop, `chkdsk C:` "found no problems" |

## Gate M7: distrorelay.exe

**Tanpa VM** (kecuali E-6). Build sekali di folder repo: `.venv\Scripts\python.exe -m pip install -c packaging\constraints.txt -e ".[dev,build]"`
lalu `.venv\Scripts\python.exe -m PyInstaller packaging\distrorelay.spec --noconfirm`:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| E-1 | Salin `dist\distrorelay.exe` ke folder lain (mis. Downloads), PowerShell biasa (bukan admin): `.\distrorelay.exe --version` lalu `.\distrorelay.exe selftest` | `DistroRelay 0.7.0`; semua `[ok]`, termasuk "exe (PyInstaller)", "Linux kit … lolos verifikasi", dan "TUI … tampil" | [x] v0.7.1, 2026-10-25, laptop: `.exe` dari `80fbd87` disalin ke folder lain, `DistroRelay 0.7.1`, `selftest` 6/6 `[ok]` dalam 2,4 detik |
| E-2 | Klik dua kali `distrorelay.exe` di Explorer | Dialog UAC → jendela baru berisi dashboard TUI + hasil scan; `q` keluar | [x] v0.7.1, 2026-10-25, laptop: dibuka lewat ShellExecute (`Invoke-Item`, sama dengan klik dua kali) → UAC → proses admin, scan TUI tercatat di log 09:38:48 (ESP di-mount lalu di-unmount), jendela ditutup pengguna sesudah ±31 detik |
| E-3 | PowerShell biasa: `.\distrorelay.exe scan` | UAC → jendela admin baru menampilkan hasil scan + "Tekan Enter untuk menutup"; jendela awal menunggu, lalu `$LASTEXITCODE` = kode keluar scan (0 kalau tidak ada error) | [x] v0.7.1, 2026-10-25, laptop: "Membuka jendela Administrator … jendela ini menunggu sampai selesai…", jendela awal menunggu 56 detik sampai Enter, `$LASTEXITCODE` 0. Hasil scan (dari `--no-elevate scan` di jendela admin) sama dengan `test_laptop_real.py`: Fedora, Arch, `grub\` = kemungkinan sisa; `disk0-p5` XBOOTLDR 2 GB `/boot` Fedora; `EFI\HP` bukan target |
| E-4 | `.\distrorelay.exe iso check`, `.\distrorelay.exe profile show`, `.\distrorelay.exe journal` | Jalan langsung di jendela yang sama, tanpa UAC | [x] v0.7.1, 2026-10-25: ketiganya tanpa UAC, exit 0 |
| E-5 | `.\distrorelay.exe kit export --ntfs`, lalu di WSL `cd /mnt/c/DistroRelay/kit && sh bootstrap.sh info` | Kit dari `.exe` lolos cek hash di Linux (sama seperti K-2) | [x] v0.7.1, 2026-10-25: 20 file, "semua hash cocok" di `kali-linux`, 9 nama logis (sesudah K-5) |
| E-6 | Di VM/PC **tanpa Python**: salin `.exe`, jalankan `selftest` dan `scan` | Sama seperti E-1/E-3, tanpa error DLL/modul | [x] v0.7.1, 2026-10-25: snapshot `windows-bersih` (tanpa Python/Guest Additions; hanya alias Store `python.exe`), `.exe` masuk lewat ISO: `--version` 0.7.1, `selftest` semua `[ok]`, `scan` → UAC → jendela admin berisi hasil scan, `$LASTEXITCODE` 0 |
| E-7 | GitHub: `git tag v0.7.0` → `git push origin v0.7.0` | Actions "Rilis exe" hijau; rilis **draft** berisi `distrorelay.exe` + `.sha256`; hash file unduhan sama; `selftest` lolos. Publish manual kalau sudah yakin | [ ] |

## Code review 2026-10-23 (branch `fix/code-review-2`)

**Tanpa VM (read-only, atau hanya menulis file DistroRelay sendiri):**

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| CR-1 | `distrorelay scan`, lalu buka log hari ini di `%LOCALAPPDATA%\DistroRelay\logs\` | Ada baris `NVRAM: 259 variabel diminta (N Boot#### ada) dalam X detik`; kirim angka X (P-2: kalau lebih dari ±1 detik, enumerasi nama variabel dibuat) | [x] v0.7.1, 2026-10-25, laptop: `NVRAM: 260 variabel diminta (5 Boot#### ada) dalam 0.076 detik` (3 scan: 0,076 / 0,029 / 0,076). Jauh di bawah 1 detik, P-2 tidak perlu |
| CR-2 | `distrorelay iso check` | 4 distro tampil; status "menunggu …" menyebut distro yang belum selesai; total jauh lebih cepat dari sebelumnya | [x] v0.7.1, 2026-10-25, laptop: 4 distro, total 5,7 detik (`.exe`) / 4,6 detik. Dengan `TTY_COMPATIBLE=1` status tertangkap: "menunggu Fedora, Arch Linux, Linux Mint" → "menunggu Arch Linux, Linux Mint" → "menunggu Linux Mint" |
| CR-3 | `distrorelay shrink-c` (tanpa `--size`) | Info sama seperti I-9; catat lamanya (post-check shrink sesudah perbaikan tidak mengulang query ini) | [x] v0.7.1, 2026-10-25, laptop: info sama seperti I-9; `shrink-c` 58,4 detik, hampir seluruhnya `Get-PartitionSupportedSize` (55,0 detik diukur terpisah) |
| CR-4 | `distrorelay journal`; kalau ada sesi lama yang masih "butuh perhatian": `distrorelay journal --tuntaskan <sesi>` → ketik `tuntas` lalu ulangi dengan `TUNTAS` | Huruf kecil ditolak; sesudah `TUNTAS` sesi tampil "ditandai tuntas …" dan banner kuning dashboard hilang | [~] v0.7.1, 2026-10-25, laptop: `journal` "Belum ada." (tidak ada sesi), jadi bagian `TUNTAS` tidak berlaku di laptop |
| CR-5 | GitHub → Actions → "Rilis exe" → Run workflow (branch ini) | Hijau dengan action yang dipin SHA + `constraints.txt`; artifact berisi `.exe` + `.sha256` | [ ] |

**Di VM (snapshot dulu):**

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| CR-6 | Buat backup ke disk eksternal VM, colok satu disk virtual lagi (atau cabut lalu colok ulang disk backup sehingga nomornya bergeser), lalu wizard uninstall sampai gerbang dengan backup itu | Gerbang lolos (B-2); kalau partisi di disk Windows diubah (mis. shrink 1 GB), gerbang menolak dan menyebut nama disknya | [~] v0.7.1, 2026-10-24: disk 1 GB dipasang di SATA port 1 (perlu reboot) → disk backup 1 → 2; gerbang TIDAK menyebut layout (B-2 ok) dan salinan E: ditemukan, tapi menolak karena `EFI\Microsoft\Boot\bootstat.dat` berubah (ditulis Windows tiap boot). Sesudah `shrink-c --size 1`: "Layout disk berubah … (VBOX HARDDISK {423ab726-…})". 2026-10-25 dengan hot-plug (tanpa reboot): disk backup 1 → 2, gerbang tidak menyebut layout dan menemukan salinan E: (B-2 ok), tapi menolak "Salinan eksternal tidak lolos verifikasi: … fsinfo.json" karena disk dicabut beberapa detik sesudah backup: salinan tidak di-flush (temuan T-6). Gerbang "lolos" dengan disk bergeser belum tercapai (re-attach hot-plug kedua ditolak VirtualBox) |
| CR-7 | Edit `verification.json` backup itu: ubah satu `"ok": true` jadi `false`, lalu `uninstall --backup <id>` | Daftar backup menandai "GAGAL verifikasi saat dibuat"; gerbang menolak (B-1); tidak ada yang berubah | [x] v0.7.1, 2026-10-24: `[GAGAL verifikasi saat dibuat]`, gerbang menyebut cek yang gagal, layout identik. Catatan: file yang disimpan dengan BOM UTF-8 (PowerShell 5.1 `Set-Content -Encoding utf8`) terbaca "verifikasi tidak tercatat" / "tidak ada atau rusak"; tetap ditolak |
| CR-8 | Uninstall sampai langkah entri boot, hentikan (Ctrl+C), lalu Pulihkan entri + folder yang sudah terhapus | Sesi uninstall tampil "dituntaskan oleh pemulihan …" (B-5), wizard berikutnya tidak meminta LANJUT | [x] v0.7.1, 2026-10-24, lewat CLI: Ctrl+C sesudah langkah 4 (Arch Boot0008 + Fedora Boot0007 terhapus) → `restore --boot-entry Boot0008 --boot-entry Boot0007` 4/4 ok (Fedora "Sudah ada" karena kembarannya Boot0005) → "dihentikan, dituntaskan oleh pemulihan …"; `uninstall` sesudahnya tidak meminta LANJUT |

## Perbaikan temuan uji 24–25 Okt (branch `fix/temuan-uji`)

Temuan T-1 sampai T-7 dan catatan C-1 sampai C-7 (tab "Plan perbaikan temuan uji" di laporan). Test otomatisnya di
[`tests/test_uji_fixes.py`](../test_uji_fixes.py). Yang di bawah butuh VM (snapshot dulu), kecuali U-5:

| # | Langkah | Yang dicek | Lolos |
|---|---|---|---|
| U-1 (T-6) | `backup --drive E` ke disk eksternal VM, lalu **langsung** cabut lewat hot-plug (tanpa Safely Remove), colok lagi, `uninstall --select … --backup <id>` sampai gerbang | Verifikasi backup memuat "[ok] Salinan USB sudah tertulis ke perangkat"; akhir backup menyebut Safely Remove; sesudah dicolok, gerbang menemukan salinan dan tidak menolak karena "Salinan eksternal tidak lolos verifikasi" | [x] 2026-10-25, Claude, `fix/temuan-uji`: backup `20261025-104431` "[ok] … 161 file di-fsync dan volume E: di-flush" + pesan Safely Remove; E: dicabut 83 ms sesudah backup selesai. Colok ulang saat VM jalan ditolak VirtualBox (bug lama), jadi E: dipasang lagi saat VM mati lalu VM dinyalakan: 9/9 file salinan identik (termasuk `fsinfo.json`), `verify-backup` E: 3/3 `[ok]`, gerbang "Semua syarat terpenuhi" |
| U-2 (T-4) | Backup, reboot VM, lalu `uninstall` sampai gerbang dengan backup itu | Gerbang tidak menolak karena ESP berubah; catatan "efi\microsoft\boot\bootstat.dat berubah … diabaikan" | [x] 2026-10-25, Claude: gerbang U-1 sesudah VM mati-nyala lolos dengan catatan itu. Temuan: post-check sesudah eksekusi masih menandai "File ESP lain tidak tersentuh" gagal karena `bootstat.dat` (sesi `20261025-120832`); diperbaiki di `postcheck.py`, sesudahnya post-check 9/9 ok walau ada reboot di antara backup dan eksekusi (sesi `122424`, `123128`) |
| U-3 (T-1) | `shrink-c --size 9999` | Hanya baris `[GAGAL]` dengan alasannya; tidak ada "Dikecilkan …" atau angka negatif | [x] 2026-10-25, Claude: dua baris `[GAGAL]` (batas Windows 28.9 GB, sisa kosong 20 GB), exit 1, layout sebelum = sesudah |
| U-4 (C-2) | Ulangi M3-4 (partisi baru dibuat sebelum `HAPUS`, sesi gagal), lalu uninstall sukses yang menghapus partisi **lain** | `journal`: sesi M3-4 tetap "butuh perhatian" (tidak "dituntaskan oleh sesi …"); sesudah uninstall yang menghapus partisi itu sendiri, baru tuntas | [x] 2026-10-25, Claude (driver `tesvm\drive.py` menjawab prompt dari teksnya): M3-4 `20261025-113353` gagal di langkah partisi (pesan C-1 baru); uninstall sukses `disk0-p8` (`122424`) → sesi M3-4 tetap "berhenti karena gagal"; uninstall sukses `disk0-p6` (`123128`) → "dituntaskan oleh sesi 20261025-123128". VM freeze (NEM) 3× saat scan, diatasi reset; snapshot `sesudah-paket-u` |
| U-5 (T-7) | WSL Kali: `sh bootstrap.sh backup` dengan kit baru | Checklist hanya berisi paket yang dipasang pengguna; catatan "N paket inti sistem / pustaka tidak dibawa" | [x] 2026-10-25, Claude, `fix/temuan-uji`: 7 kandidat (sebelumnya 134), "140 paket inti sistem / pustaka tidak dibawa"; `profile import` hanya menawarkan 7 paket itu |

## Khusus M2a (sudah bisa dicoba sekarang)

- [ ] `distrorelay backup --usb <huruf>` di VM: semua verifikasi `[ok]`, folder `esp\` di USB lengkap
- [ ] Ubah satu file di salinan USB → verifikasi ulang mendeteksi hash tidak cocok
- [ ] `distrorelay plan --select ...` menampilkan urutan: Windows pertama → fallback → entri → folder
